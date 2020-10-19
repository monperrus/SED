import collections
import math
import sys
import os
import time
from itertools import count

import numpy as np

import torch
import torch.nn as nn
from torch import optim
import torch.nn.functional as F

#from pytorch_tools import torchfold

from datasets import data, executor
from tools import saver


class MaskedMemory(collections.namedtuple('MaskedMemory', ['memory',
    'attn_mask'])):

    def expand_by_beam(self, beam_size):
        return MaskedMemory(*(v.unsqueeze(1).repeat(1, beam_size, *([1] * (
            v.dim() - 1))).view(-1, *v.shape[1:]) for v in self))

    def apply(self, fn):
        return MaskedMemory(fn(self.memory), fn(self.attn_mask))


def get_attn_mask(seq_lengths, cuda):
    max_length, batch_size = max(seq_lengths), len(seq_lengths)
    ranges = torch.arange(
        0, max_length,
        out=torch.LongTensor()).unsqueeze(0).expand(batch_size, -1)
    attn_mask = (ranges >= torch.LongTensor(seq_lengths).unsqueeze(1))
    if cuda:
        attn_mask = attn_mask.cuda()
    return attn_mask


class InferenceResult(object):

    def __init__(self, code_tree=None, code_sequence=None, info=None):
        self.code_tree = code_tree
        self.code_sequence = code_sequence
        self.info = info

    def to_dict(self):
        return {
            'info': self.info,
            'code_tree': self.code_tree if self.code_tree else [],
            'code_sequence': self.code_sequence,
        }

    @staticmethod
    def dovetail(inference_results):
        """
        Combines several inference results together via dovetailing. Only works if the `info` contains
        beams of the form info['trees_checked'], and info['candidates'] == [list of candidates,,,]
        """
        assert inference_results
        code_tree = inference_results[0].code_tree
        code_sequence = inference_results[0].code_sequence
        assert all(res.info.keys() == {'trees_checked', 'candidates'} for res in inference_results)
        candidates = []
        for i in count():
            done = True
            for res in inference_results:
                if i < len(res.info['candidates']):
                    candidates.append(res.info['candidates'][i])
                    done = False
            if done:
                break
        trees_checked = sum(res.info['trees_checked'] for res in inference_results)
        return InferenceResult(code_tree=code_tree, code_sequence=code_sequence, info=dict(trees_checked=trees_checked, candidates=candidates))

class BaseModel(object):

    def __init__(self, args):
        self.args = args
        self.model_dir = args.model_dir
        self.save_every_n = args.save_every_n
        self.debug_every_n = args.debug_every_n

        self.saver = saver.Saver(self.model, self.optimizer, args.keep_every_n)
        self.last_step = self.saver.restore(
            self.model_dir, map_to_cpu=args.restore_map_to_cpu,
            step=getattr(args, 'step', None))
        if self.last_step == 0 and args.pretrained:
            for kind_path in args.pretrained.split(':_:'):
                kind, path = kind_path.split('::')
                self.load_pretrained(kind, path)

    def load_pretrained(self, kind, path):
        if kind == 'entire-model':
            keep_weight = lambda x: True
        elif kind == 'encoder':
            keep_weight = lambda x: {'encoder': True, 'code_encoder': True, 'decoder': False, 'optimizer': False}[
                x.split(".")[0]]
        else:
            raise NotImplementedError

        step = self.saver.restore(path, map_to_cpu=self.args.restore_map_to_cpu,
                                  step=self.args.pretrained_step, keep_weight=keep_weight)
        assert step == self.args.pretrained_step, "Step {} of model {} does not work".format(path,
                                                                                             self.args.pretrained_step)


    def compute_loss(self, batch):
        raise NotImplementedError

    def inference(self, batch):
        raise NotImplementedError

    def debug(self, batch):
        raise NotImplementedError

    def train(self, batch):
        self.update_lr()
        self.optimizer.zero_grad()
        loss = self.compute_loss(batch)
        loss.backward()
        if self.args.gradient_clip is not None and self.args.gradient_clip > 0:
            nn.utils.clip_grad_norm(self.model.parameters(),
                                    self.args.gradient_clip)
        self.optimizer.step()
        self.last_step += 1
        if self.debug_every_n > 0 and self.last_step % self.debug_every_n == 0:
            self.debug(batch)
        if self.last_step % self.save_every_n == 0:
            self.saver.save(self.model_dir, self.last_step)
        return {'loss': loss.data.item()}

    def eval(self, batch):
        results = self.inference(batch)
        correct = 0
        for example, res in zip(batch, results):
            if example.code_sequence == res.code_sequence or example.code_tree == res.code_tree:
                correct += 1
        return {'correct': correct, 'total': len(batch)}

    def update_lr(self):
        args = self.args
        if args.lr_decay_steps is None or args.lr_decay_rate is None:
            return

        lr = args.lr * args.lr_decay_rate ** (self.last_step //
                                              args.lr_decay_steps)
        for param_group in self.optimizer.param_groups:
            param_group['lr'] = lr

    def batch_processor(self, for_eval):
        '''Returns a function used to process batched data for this class.'''
        def default_processor(batch):
            return batch
        return default_processor


class BaseCodeModel(BaseModel):

    def __init__(self, args):
        self.criterion = nn.CrossEntropyLoss(ignore_index=-1)
        if args.optimizer == 'adam':
            self.optimizer = optim.Adam(self.model.parameters(), lr=args.lr)
        elif args.optimizer == 'sgd':
            self.optimizer = optim.SGD(self.model.parameters(), lr=args.lr)
        else:
            raise ValueError(args.optimizer)

        super(BaseCodeModel, self).__init__(args)

        if args.cuda:
            self.model.cuda()
        print(self.model)

    def reset_vocab(self):
        self.last_vocab = data.PlaceholderVocab(
            self.vocab, self.args.num_placeholders)
        return self.last_vocab

    def _try_sequences(self, vocab, sequences, batch, beam_size):
        result = [[] for _ in range(len(batch))]
        counters = [0 for _ in range(len(batch))]
        candidates = [[] for _ in range(len(batch))]
        max_eval_trials = self.args.max_eval_trials or beam_size
        for batch_id, outputs in enumerate(sequences):
            example = batch[batch_id]
            #print("===", example.code_tree)
            candidates[batch_id] = [[vocab.itos(idx) for idx in ids]
                                    for ids in outputs]
            for code in candidates[batch_id][:max_eval_trials]:
                counters[batch_id] += 1
                stats = executor.evaluate_code(
                    code, example.schema.args, example.input_tests, self.executor.execute)
                ok = (stats['correct'] == stats['total'])
                #print(code, stats)
                if ok:
                    result[batch_id] = code
                    break
        return [InferenceResult(code_sequence=seq, info={'trees_checked': c, 'candidates': cand})
                for seq, c, cand in zip(result, counters, candidates)]
