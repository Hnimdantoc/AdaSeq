# Copyright (c) Alibaba, Inc. and its affiliates.
from typing import Any, Dict, List, Optional, Union

import torch
import torch.nn as nn
import torch.nn.functional as F
from modelscope.models.builder import MODELS
from modelscope.utils.config import ConfigDict

from adaseq.data.constant import PAD_LABEL_ID
from adaseq.metainfo import Models
from adaseq.models.base import Model
from adaseq.modules.decoders import CRF, PartialCRF
from adaseq.modules.dropouts import WordDropout
from adaseq.modules.encoders import Encoder
from adaseq.modules.util import get_tokens_mask


@MODELS.register_module(module_name=Models.sequence_labeling_model)
class SequenceLabelingModel(Model):
    """Sequence labeling model

    This model is used for sequence labeling tasks.
    Various decoders are supported, including argmax, crf, partial-crf, etc.

    Args:
        num_labels (int): number of labels
        encoder (Union[Encoder, str], `optional`): encoder used in the model.
            It can be an `Encoder` instance or an encoder config file or an encoder config dict.
        word_dropout (float, `optional`): word dropout rate, default `0.0`.
        use_crf (bool, `optional`): whether to use crf, default `True`.
        **kwargs: other arguments
    """

    def __init__(
        self,
        id_to_label: Dict[int, str],
        encoder: Union[Encoder, str, ConfigDict] = None,
        word_dropout: Optional[float] = 0.0,
        use_crf: Optional[bool] = True,
        multiview: Optional[bool] = False,
        temperature: Optional[float] = 1.0,
        mv_loss_type: Optional[str] = 'kl',
        mv_interpolation: Optional[float] = 0.5,
        partial: Optional[bool] = False,
        **kwargs
    ) -> None:
        super().__init__()
        self.id_to_label = id_to_label
        self.num_labels = len(id_to_label)
        if isinstance(encoder, Encoder):
            self.encoder = encoder
        else:
            self.encoder = Encoder.from_config(cfg_dict_or_path=encoder)
        self.linear = nn.Linear(self.encoder.config.hidden_size, self.num_labels)

        self.use_dropout = word_dropout > 0.0
        if self.use_dropout:
            self.dropout = WordDropout(word_dropout)

        self.use_crf = use_crf
        if use_crf:
            if partial:
                self.crf = PartialCRF(self.num_labels, batch_first=True)
            else:
                self.crf = CRF(self.num_labels, batch_first=True)
        else:
            self.loss_fn = nn.CrossEntropyLoss(reduction='mean', ignore_index=PAD_LABEL_ID)

        self.multiview = multiview
        self.mv_loss_type = mv_loss_type
        self.temperature = temperature
        self.mv_interpolation = mv_interpolation

    def _forward(self, tokens: Dict[str, Any]) -> torch.Tensor:
        embed = self.encoder(**tokens)

        if self.use_dropout:
            embed = self.dropout(embed)

        logits = self.linear(embed)

        return logits

    def forward(  # noqa
        self,
        tokens: Dict[str, Any],
        label_ids: Optional[torch.LongTensor] = None,
        meta: Optional[Dict[str, Any]] = None,
        origin_tokens: Optional[Dict[str, Any]] = None,
        origin_mask: Optional[torch.Tensor] = None,
    ) -> Dict[str, Any]:  # TODO docstring
        logits = self._forward(tokens)

        mask = get_tokens_mask(tokens, logits.size(1))

        if self.training:
            crf_mask = origin_mask if origin_mask is not None else mask
            loss = self._calculate_loss(logits, label_ids, crf_mask)

            if self.multiview and origin_tokens is not None:  # for multiview training
                origin_view_logits = self._forward(origin_tokens)

                origin_mask = get_tokens_mask(origin_tokens, origin_view_logits.size(1))

                origin_view_loss = self._calculate_loss(origin_view_logits, label_ids, origin_mask)
                if self.mv_loss_type == 'kl':
                    cl_kl_loss = self._calculate_cl_loss(
                        logits, origin_view_logits, mask, T=self.temperature
                    )
                    loss = (
                        self.mv_interpolation * (loss + origin_view_loss)
                        + (1 - self.mv_interpolation) * cl_kl_loss
                    )
                elif self.mv_loss_type == 'crf_kl':
                    cl_kl_loss = self._calculate_cl_loss(
                        logits, origin_view_logits, mask, T=self.temperature
                    )
                    loss = (
                        self.mv_interpolation * (loss + origin_view_loss)
                        + (1 - self.mv_interpolation) * cl_kl_loss
                    )
            outputs = {'logits': logits, 'loss': loss}
        else:
            predicts = self.decode(logits, mask)
            outputs = {'logits': logits, 'predicts': predicts}

        return outputs

    def _calculate_loss(
        self, logits: torch.Tensor, targets: torch.Tensor, mask: torch.Tensor
    ) -> torch.Tensor:
        if self.use_crf:
            targets = targets * mask
            loss = -self.crf(logits, targets, reduction='mean', mask=mask)
        else:
            loss = self.loss_fn(logits.transpose(1, 2), targets)
        return loss

    def _calculate_cl_loss(self, ext_view_logits, origin_view_logits, mask, T=1.0):
        if self.multiview:
            batch_size, max_seq_len, num_classes = ext_view_logits.shape
            ext_view_logits = ext_view_logits.detach()
            if self.mv_loss_type == 'kl':
                _loss = (
                    F.kl_div(
                        F.log_softmax(origin_view_logits / T, dim=-1),
                        F.softmax(ext_view_logits / T, dim=-1),
                        reduction='none',
                    )
                    * mask.unsqueeze(-1)
                    * T
                    * T
                )
            elif self.mv_loss_type == 'crf_kl':
                if self.use_crf:
                    origin_view_log_posterior = self.crf.compute_posterior(origin_view_logits, mask)
                    ext_view_log_posterior = self.crf.compute_posterior(ext_view_logits, mask)
                    _loss = (
                        F.kl_div(
                            F.log_softmax(origin_view_log_posterior / T, dim=-1),
                            F.softmax(ext_view_log_posterior / T, dim=-1),
                            reduction='none',
                        )
                        * mask.unsqueeze(-1)
                        * T
                        * T
                    )
                else:
                    raise NotImplementedError
            else:
                raise NotImplementedError
            loss = _loss.sum() / batch_size
        else:
            loss = 0.0
        return loss

    def decode(  # noqa: D102
        self, logits: torch.Tensor, mask: torch.Tensor
    ) -> Union[List, torch.LongTensor]:
        if self.use_crf:
            predicts = self.crf.decode(logits, mask=mask).squeeze(0)
        else:
            predicts = logits.argmax(-1)
        return predicts
