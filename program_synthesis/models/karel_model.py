import collections
import itertools
import numpy as np
import operator
import traceback

import torch
from torch import nn
from torch.autograd import Variable

from .base import BaseCodeModel, InferenceResult
from datasets import data, dataset, executor
from .modules import karel
from tools import edit
from . import beam_search
from . import prepare_spec


def code_to_tokens(seq, vocab):
    tokens = []
    for i in seq:
        if i == 1:  # </s>
            break
        tokens.append(vocab.itos(i))
    return tokens


def encode_grids_and_outputs(batch, vocab):
    # TODO: Don't hard-code 5 I/O examples
    input_grids, output_grids = [
        torch.zeros(len(batch), 5, 15, 18, 18) for _ in range(2)
    ]
    for batch_idx, item in enumerate(batch):
        assert len(item.input_tests) == 5, len(item.input_tests)
        for test_idx, test in enumerate(item.input_tests):
            inp, out = test['input'], test['output']
            input_grids[batch_idx, test_idx].view(-1)[inp] = 1
            output_grids[batch_idx, test_idx].view(-1)[out] = 1
    input_grids, output_grids = [
        Variable(t) for t in (input_grids, output_grids)
    ]
    code_seqs = prepare_spec.lists_padding_to_tensor(
        [item.code_sequence for item in batch], vocab.stoi, False)
    return input_grids, output_grids, code_seqs

def maybe_cuda(tensor, async=False):
    if tensor is None:
        return None
    return tensor.cuda(async=async)


def denumpify(item):
    if type(item) in {tuple, list, np.ndarray}: # no isinstance to avoid named tuples
        return [int(x) if np.issubdtype(type(x), np.integer) else x for x in item]
    else:
        return item

def lists_to_packed_sequence(lists, item_shape, tensor_type, item_to_tensor):
    # TODO: deduplicate with the version in prepare_spec.
    result = tensor_type(sum(len(lst) for lst in lists), *item_shape)

    sorted_lists, sort_to_orig, orig_to_sort = prepare_spec.sort_lists_by_length(lists)
    lengths = prepare_spec.lengths(sorted_lists)
    batch_bounds = prepare_spec.batch_bounds_for_packing(lengths)
    idx = 0
    for i, bound in enumerate(batch_bounds):
        for batch_idx, lst  in enumerate(sorted_lists[:bound]):
            try:
                item_to_tensor(denumpify(lst[i]), batch_idx, result[idx])
            except:
                print(lst[i])
                print(type(lst[i]) == tuple)
                1/0
            idx += 1

    result = Variable(result)
    batch_bounds = torch.tensor(batch_bounds, dtype=torch.long)

    return prepare_spec.PackedSequencePlus(
            nn.utils.rnn.PackedSequence(result, batch_bounds),
            lengths, sort_to_orig, orig_to_sort)


def interleave(source_lists, interleave_indices):
    result = []

    try:
        source_iters = [iter(lst) for lst in source_lists]
        for i in interleave_indices:
            result.append(next(source_iters[i]))
    except StopIteration:
        raise Exception('source_lists[{}] ended early'.format(i))

    for it in source_iters:
        ended = False
        try:
            next(it)
        except StopIteration:
            ended = True
        assert ended

    return result


class BaseKarelModel(BaseCodeModel):
    def eval(self, batch):
        results = self.inference(batch)
        correct = 0
        code_seqs = batch.code_seqs.cpu()
        for code_seq, res in zip(code_seqs, results):
            code_tokens = code_to_tokens(list(np.array(code_seq.data[1:])), self.vocab)
            if code_tokens == res.code_sequence:
                correct += 1
        return {'correct': correct, 'total': len(code_seqs)}

    def _try_sequences(self, vocab, sequences, input_grids, output_grids,
                       beam_size):
        result = [[] for _ in range(len(sequences))]
        counters = [0 for _ in range(len(sequences))]
        candidates = [[] for _ in range(len(sequences))]
        max_eval_trials = self.args.max_eval_trials or beam_size
        for batch_id, outputs in enumerate(sequences):
            input_tests = [
                {
                    'input': np.where(inp.numpy().ravel())[0].tolist(),
                    'output': np.where(out.numpy().ravel())[0].tolist(),
                }
                for inp, out in zip(
                    torch.split(input_grids[batch_id].data.cpu(), 1),
                    torch.split(output_grids[batch_id].data.cpu(), 1), )
            ]
            candidates[batch_id] = [[vocab.itos(idx) for idx in ids]
                                    for ids in outputs]
            for code in candidates[batch_id][:max_eval_trials]:
                counters[batch_id] += 1
                stats = executor.evaluate_code(code, None, input_tests,
                                               self.executor.execute)
                ok = (stats['correct'] == stats['total'])
                if ok:
                    result[batch_id] = code
                    break
        return [
            InferenceResult(
                code_sequence=seq,
                info={'trees_checked': c,
                      'candidates': cand})
            for seq, c, cand in zip(result, counters, candidates)
        ]


class KarelLGRLModel(BaseKarelModel):
    def __init__(self, args):
        self.args = args
        if not hasattr(self.args, 'train_policy_gradient_loss'):
            self.args.train_policy_gradient_loss = False
        self.vocab = data.PlaceholderVocab(
            data.load_vocab(args.word_vocab), self.args.num_placeholders)
        self.model = karel.LGRLKarel(
            len(self.vocab) - self.args.num_placeholders, args)
        self.executor = executor.get_executor(args)()
        super(KarelLGRLModel, self).__init__(args)

    def compute_loss(self, input_tuple):
        input_grids, output_grids, code_seqs, _ = input_tuple
        if self.args.cuda:
            input_grids = input_grids.cuda(async=True)
            output_grids = output_grids.cuda(async=True)
            code_seqs = code_seqs.cuda(async=True)
        # io_embeds shape: batch size x num pairs (5) x hidden size (512)
        io_embed = self.model.encode(input_grids, output_grids)
        logits, labels = self.model.decode(io_embed, code_seqs)
        return self.criterion(
            logits.view(-1, logits.shape[-1]), labels.contiguous().view(-1))

    def debug(self, batch):
        batch = [x[0:1] for x in batch]
        code = code_to_tokens(batch.code_seqs.data[0, 1:], self.vocab)
        print("Code: %s" % ' '.join(code))
        res, = self.inference(batch)
        print("Out:  %s" % ' '.join(res.code_sequence))

    def inference(self, input_tuple):
        input_grids, output_grids, _, _ = input_tuple
        if self.args.cuda:
            input_grids = input_grids.cuda(async=True)
            output_grids = output_grids.cuda(async=True)

        io_embed = self.model.encode(input_grids, output_grids)
        init_state = karel.LGRLDecoderState(*self.model.decoder.init_state(
            io_embed.shape[0], io_embed.shape[1]))
        memory = karel.LGRLMemory(io_embed)

        sequences = beam_search.beam_search(
            len(input_grids),
            init_state,
            memory,
            self.model.decode_token,
            self.args.max_beam_trees,
            cuda=self.args.cuda,
            max_decoder_length=self.args.max_decoder_length,
            return_attention=False,
            return_beam_search_result=False,
            differentiable=False,
            use_length_penalty=self.args.use_length_penalty,
            factor = self.args.length_penalty_factor)

        return self._try_sequences(self.vocab, sequences, input_grids,
                                   output_grids, self.args.max_beam_trees)

    def batch_processor(self, for_eval):
        return KarelLGRLBatchProcessor(self.vocab, for_eval, self.args.train_policy_gradient_loss)


KarelLGRLExample = collections.namedtuple(
    'KarelLGRLExample',
    ('input_grids', 'output_grids', 'code_seqs', 'orig_examples'))


class KarelLGRLBatchProcessor(object):
    def __init__(self, vocab, for_eval, train_policy_gradient_loss):
        self.vocab = vocab
        self.for_eval = for_eval
        self.train_policy_gradient_loss = train_policy_gradient_loss

    def __call__(self, batch):
        input_grids, output_grids, code_seqs = encode_grids_and_outputs(
            batch, self.vocab)
        orig_examples = batch if self.for_eval or self.train_policy_gradient_loss else None
        return KarelLGRLExample(input_grids, output_grids, code_seqs,
                                orig_examples)


class KarelLGRLRefineModel(BaseKarelModel):
    def __init__(self, args):
        self.args = args
        self.vocab = data.PlaceholderVocab(
            data.load_vocab(args.word_vocab), self.args.num_placeholders)
        self.model = karel.LGRLRefineKarel(
            len(self.vocab) - self.args.num_placeholders, args)
        if args.cuda:
            self.model = self.model.cuda()
        self.executor = executor.get_executor(args)()

        self.trace_grid_lengths = []
        self.trace_event_lengths  = []
        self.trace_lengths = []
        super(KarelLGRLRefineModel, self).__init__(args)

    def compute_loss(self, input_tuple):
        input_grids, output_grids, code_seqs, dec_data, \
                            ref_code, ref_trace_grids, ref_trace_events, \
                            cag_interleave, orig_examples = input_tuple
        # TODO before the policy gradient this was impossible to execute since `orig_examples` is None whenever
        # this is not for_eval. Excluding the policy gradient
        if orig_examples and not self.args.train_policy_gradient_loss:
            for i, orig_example in  enumerate(orig_examples):
                self.trace_grid_lengths.append((orig_example.idx, [
                    ref_trace_grids.lengths[ref_trace_grids.sort_to_orig[i * 5
                                                                         + j]]
                    for j in range(5)
                ]))
                self.trace_event_lengths.append((orig_example.idx, [
                    len(ref_trace_events.interleave_indices[i * 5 + j])
                    for j in range(5)
                ]))
                self.trace_lengths.append(
                    (orig_example.idx, np.array(self.trace_grid_lengths[-1][1])
                     + np.array(self.trace_event_lengths[-1][1])))

        if self.args.cuda:
            input_grids = input_grids.cuda(async=True)
            output_grids = output_grids.cuda(async=True)
            code_seqs = maybe_cuda(code_seqs, async=True)
            dec_data = maybe_cuda(dec_data, async=True)
            ref_code = maybe_cuda(ref_code, async=True)
            ref_trace_grids = maybe_cuda(ref_trace_grids, async=True)
            ref_trace_events = maybe_cuda(ref_trace_events, async=True)

        # io_embeds shape: batch size x num pairs (5) x hidden size (512)
        io_embed, ref_code_memory, ref_trace_memory = self.model.encode(
            input_grids, output_grids, ref_code, ref_trace_grids,
            ref_trace_events, cag_interleave)

        if self.args.train_policy_gradient_loss:
            return self.calculate_policy_gradient_loss(input_grids, io_embed, orig_examples, ref_code, ref_code_memory,
                                                       ref_trace_memory)

        else:
            return self.calculate_supervised_loss(io_embed, ref_code_memory,
                                                  ref_trace_memory, code_seqs,
                                                  dec_data)

    def calculate_supervised_loss(self, io_embed, ref_code_memory,
                                  ref_trace_memory, code_seqs,
                                  dec_data):
        logits, labels = self.model.decode(io_embed, ref_code_memory,
                                           ref_trace_memory, code_seqs,
                                           dec_data)
        return self.criterion(
            logits.view(-1, logits.shape[-1]), labels.contiguous().view(-1))

    def calculate_policy_gradient_loss(self, input_grids, io_embed, orig_examples, ref_code, ref_code_memory,
                                       ref_trace_memory):
        init_state = self.model.decoder.init_state(
            ref_code_memory, ref_trace_memory,
            io_embed.shape[0], io_embed.shape[1])
        memory = self.model.decoder.prepare_memory(io_embed, ref_code_memory,
                                                   ref_trace_memory, ref_code)
        sequences = beam_search.beam_search(
            len(input_grids),
            init_state,
            memory,
            self.model.decode_token,
            self.args.max_beam_trees,
            cuda=self.args.cuda,
            max_decoder_length=self.args.max_decoder_length,
            return_beam_search_result=True,
            volatile=False,
            differentiable=True,
            use_length_penalty=self.args.use_length_penalty,
            factor=self.args.length_penalty_factor
        )
        output_code = self.model.decoder.postprocess_output([[x.sequence for x in y] for y in sequences], memory)
        all_logits = []
        rewards = []
        for logit_beam, code_beam, example in zip(sequences, output_code, orig_examples):
            for i, (logits, code) in enumerate(zip(logit_beam, code_beam)):
                code = list(map(self.vocab.itos, code))
                all_logits.append(torch.sum(torch.cat([x.view(1) for x in logits.log_probs_torch])))
                run_cases = lambda tests: executor.evaluate_code(code, example.schema.args, tests,
                                                                 self.executor.execute)
                input_tests = run_cases(example.input_tests)
                reward = input_tests['correct'] / input_tests['total']
                if self.args.use_held_out_test_for_rl:
                    held_out_test = run_cases(example.tests)
                    reward += held_out_test['correct']  # worth as much as all the other ones combined
                rewards.append(reward)
        all_logits = torch.cat([x.view(1) for x in all_logits])
        print(np.mean(rewards))
        rewards = torch.tensor(rewards)
        if not self.args.no_baseline:
            rewards = rewards - np.mean(rewards)
        if all_logits.is_cuda:
            rewards = rewards.cuda()
        return - (rewards * all_logits).mean()

    def debug(self, batch):
        code = code_to_tokens(batch.code_seqs.data[0, 1:], self.vocab)
        print("Code: %s" % ' '.join(code))

    def inference(self, input_tuple):
        input_grids, output_grids, _1, dec_data, ref_code, \
                      ref_trace_grids, ref_trace_events, cag_interleave, _2 = input_tuple
        if self.args.cuda:
            input_grids = input_grids.cuda(async=True)
            output_grids = output_grids.cuda(async=True)
            dec_data = maybe_cuda(dec_data, async=True)
            ref_code = maybe_cuda(ref_code, async=True)
            ref_trace_grids = maybe_cuda(ref_trace_grids, async=True)
            ref_trace_events = maybe_cuda(ref_trace_events, async=True)

        io_embed, ref_code_memory, ref_trace_memory = self.model.encode(
            input_grids, output_grids, ref_code, ref_trace_grids,
            ref_trace_events, cag_interleave)
        init_state = self.model.decoder.init_state(
                ref_code_memory, ref_trace_memory,
                io_embed.shape[0], io_embed.shape[1])
        memory = self.model.decoder.prepare_memory(io_embed, ref_code_memory,
                                                   ref_trace_memory, ref_code)

        sequences = beam_search.beam_search(
            len(input_grids),
            init_state,
            memory,
            self.model.decode_token,
            self.args.max_beam_trees,
            cuda=self.args.cuda,
            max_decoder_length=self.args.max_decoder_length,
            return_attention=False,
            return_beam_search_result=False,
            differentiable=False,
            use_length_penalty=self.args.use_length_penalty,
            factor = self.args.length_penalty_factor)

        sequences = self.model.decoder.postprocess_output(sequences, memory)

        return self._try_sequences(self.vocab, sequences, input_grids,
                                   output_grids, self.args.max_beam_trees)

    def batch_processor(self, for_eval):
        return KarelLGRLRefineBatchProcessor(self.args, self.vocab, for_eval)


class KarelLGRLOverfitModel(BaseKarelModel):
    def __init__(self, args):
        self.args = args

        self.vocab = data.PlaceholderVocab(
            data.load_vocab(args.word_vocab), self.args.num_placeholders)
        self.model = karel.LGRLClassifierKarel(
            len(self.vocab) - self.args.num_placeholders, args)
        if args.cuda:
            self.model = self.model.cuda()

        self.loss_function = torch.nn.CrossEntropyLoss()
        super(KarelLGRLOverfitModel, self).__init__(args)

    def common_forward(self, input_tuple):
        input_grids, output_grids, _1, dec_data, ref_code, \
                      ref_trace_grids, ref_trace_events, cag_interleave, _2 = input_tuple
        if self.args.cuda:
            input_grids = input_grids.cuda(async=True)
            output_grids = output_grids.cuda(async=True)
            dec_data = maybe_cuda(dec_data, async=True)
            ref_code = maybe_cuda(ref_code, async=True)
            ref_trace_grids = maybe_cuda(ref_trace_grids, async=True)
            ref_trace_events = maybe_cuda(ref_trace_events, async=True)

        return self.model(
            input_grids, output_grids, ref_code, ref_trace_grids,
            ref_trace_events, cag_interleave)

    def get_labels(self, orig_examples):
        return [int(eg.ref_example.code_is_correct) for eg in orig_examples]

    def compute_loss(self, input_tuple):
        input_grids, output_grids, code_seqs, dec_data, \
                            ref_code, ref_trace_grids, ref_trace_events, \
                            cag_interleave, orig_examples = input_tuple
        logits = self.common_forward(input_tuple)
        labels = torch.tensor(self.get_labels(orig_examples))
        if logits.is_cuda:
            labels = labels.cuda()
        loss_val = self.loss_function(logits, labels)
        return loss_val

    def inference(self, input_tuple):
        results = self.common_forward(input_tuple)
        return results[:, 1] - results[:, 0]

    def debug(self, batch):
        yhat = (self.inference(batch) > 0).cpu().numpy().astype(np.bool)
        y = np.array(self.get_labels(batch.orig_examples), dtype=np.bool)

        false_positives = np.sum(yhat & ~y)
        false_negatives = np.sum(~yhat & y)
        true_positives = np.sum(yhat & y)
        true_negatives = np.sum(~yhat & ~y)

        print("False positives: ", false_positives)
        print("True positives: ", true_positives)
        print("False negatives: ", false_negatives)
        print("True negatives: ", true_negatives)
        print("Accuracy: ", np.mean(yhat == y))

    def batch_processor(self, for_eval):
        return KarelLGRLRefineBatchProcessor(self.args, self.vocab, for_eval)


KarelLGRLRefineExample = collections.namedtuple('KarelLGRLRefineExample', (
    'input_grids', 'output_grids', 'code_seqs', 'dec_data',
    'ref_code', 'ref_trace_grids', 'ref_trace_events',
    'cond_action_grid_interleave', 'orig_examples'))


class PackedTrace(collections.namedtuple('PackedTrace', ('actions',
    'action_code_indices', 'conds', 'cond_code_indices',
    'interleave_indices'))):
    def cuda(self, async=False):
        actions = maybe_cuda(self.actions, async)
        action_code_indices = maybe_cuda(self.action_code_indices, async)
        conds = maybe_cuda(self.conds, async)
        cond_code_indices = maybe_cuda(self.cond_code_indices, async)

        return PackedTrace(actions, action_code_indices, conds,
                cond_code_indices, self.interleave_indices)


class Spans(collections.namedtuple('Spans', 'spans')):
    def cuda(self, async=False):
        return self

class PackedDecoderData(collections.namedtuple('PackedDecoderData', ('input',
    'output', 'io_embed_indices', 'ref_code'))):
    def cuda(self, async=False):
        input_ = maybe_cuda(self.input, async)
        output = maybe_cuda(self.output, async)
        io_embed_indices = maybe_cuda(self.io_embed_indices, async)
        ref_code = maybe_cuda(self.ref_code, async)
        return PackedDecoderData(input_, output, io_embed_indices, ref_code)


class KarelLGRLRefineBatchProcessor(object):
    def __init__(self, args, vocab, for_eval):
        self.args = args
        self.vocab = vocab
        self.for_eval = for_eval
        self.return_edits = getattr(self.args, 'return_edits', False)

    def __call__(self, batch):
        input_grids, output_grids, code_seqs = encode_grids_and_outputs(
            batch, self.vocab)

        if self.args.karel_code_enc == 'none':
            ref_code = None
        else:
            append_eos = self.args.karel_refine_dec == 'edit'
            ref_code = prepare_spec.lists_to_packed_sequence(
                [item.ref_example.code_sequence + ('</S>',) for item in batch]
                if append_eos else
                [item.ref_example.code_sequence for item in batch],
                self.vocab.stoi,
                False,
                volatile=False)

        if self.args.karel_refine_dec == 'edit':
            dec_data = self.compute_edit_ops(batch, ref_code, self.return_edits)
        else:
            dec_data = None

        if self.args.karel_trace_enc.startswith('aggregate'):
            ref_trace_grids = self.prepare_traces_grids(batch)
            ref_trace_events = self.get_spans(batch, ref_code)
            cag_interleave = None
        elif self.args.karel_trace_enc == 'none':
            ref_trace_grids, ref_trace_events = None, None
            cag_interleave = None
        else:
            ref_trace_grids = self.prepare_traces_grids(batch)
            ref_trace_events = self.prepare_traces_events(batch, ref_code)

            cag_interleave = []
            grid_lengths = [ref_trace_grids.lengths[i] for i in
                    ref_trace_grids.sort_to_orig]
            for idx, (
                    grid_length, trace_interleave,
                    g_ca_interleave) in enumerate(
                        zip(grid_lengths, ref_trace_events.interleave_indices,
                            self.interleave_grids_events(batch))):
                cag_interleave.append(
                    interleave([[2] * grid_length, trace_interleave],
                               g_ca_interleave))

        orig_examples = batch if self.for_eval or self.args.train_policy_gradient_loss or self.args.model_type == 'karel-lgrl-overfit' else None

        if self.args.use_ref_orig:
            orig_examples = prepare_spec.numpy_to_tensor(prepare_spec.lists_to_numpy([('<S>',) + item.ref_example.code_sequence +('</S>',) for item in batch], self.vocab.stoi,-1),False,False)

        return KarelLGRLRefineExample(
            input_grids, output_grids, code_seqs, dec_data,
            ref_code, ref_trace_grids, ref_trace_events, cag_interleave,
            orig_examples)

    def compute_edit_ops(self, batch, ref_code, return_edits=False):
        # Sequence length: 2 + len(edit_ops)
        #
        # Op encoding:
        #   0: <s>
        #   1: </s>
        #   2: keep
        #   3: delete
        #   4: insert vocab 0
        #   5: replace vocab 0
        #   6: insert vocab 1
        #   7: replace vocab 1
        #   ...
        #
        # Inputs to RNN:
        # - <s> + op
        # - emb from source position + </s>
        # - <s> + last generated token (or null if last action was deletion)
        #
        # Outputs of RNN:
        # - op + </s>
        edit_lists = []
        for batch_idx, item in enumerate(batch):
            edit_ops =  list(
                    edit.compute_edit_ops(item.ref_example.code_sequence,
                        item.code_sequence, self.vocab.stoi))
            dest_iter = itertools.chain(['<s>'], item.code_sequence)

            # Op = <s>, emb location, last token = <s>
            source_locs, ops, values = [list(x) for x in zip(*edit_ops)]
            source_locs.append(len(item.ref_example.code_sequence))
            ops = [0] + ops
            values = [None] + values

            edit_list = []
            op_idx = 0
            for source_loc, op, value in zip(source_locs, ops, values):
                if op == 'keep':
                    op_idx = 2
                elif op == 'delete':
                    op_idx = 3
                elif op == 'insert':
                    op_idx = 4 + 2 * self.vocab.stoi(value)
                elif op == 'replace':
                    op_idx = 5 + 2 * self.vocab.stoi(value)
                elif isinstance(op, int):
                    op_idx = op
                else:
                    raise ValueError(op)

                # Set last token to UNK if operation is delete
                # XXX last_token should be 0 (<s>) at the beginning
                try:
                    last_token = 2 if op_idx == 3 else self.vocab.stoi(
                            next(dest_iter))
                except StopIteration:
                    raise Exception('dest_iter ended early')

                assert source_loc < ref_code.lengths[ref_code.sort_to_orig[batch_idx]]
                edit_list.append((
                    op_idx, ref_code.raw_index(batch_idx, source_loc),
                    last_token))
            stopped = False
            try:
                next(dest_iter)
            except StopIteration:
                stopped = True
            assert stopped

            # Op = </s>, emb location and last token are irrelevant
            edit_list.append((1, None, None))
            edit_lists.append(edit_list)

        rnn_inputs = lists_to_packed_sequence(
                [lst[:-1] for lst in edit_lists], (3,), torch.LongTensor,
                lambda op_emb_pos_last_token, _, out:
                out.copy_(torch.LongTensor([*op_emb_pos_last_token])))
        rnn_outputs = lists_to_packed_sequence(
                [lst[1:] for lst in edit_lists], (1,), torch.LongTensor,
                lambda op_emb_pos_last_token, _, out:
                out.copy_(torch.LongTensor([op_emb_pos_last_token[0]])))

        io_embed_indices = torch.LongTensor([
            expanded_idx
            for b in rnn_inputs.ps.batch_sizes
            for orig_idx in rnn_inputs.orig_to_sort[:b]
            for expanded_idx in range(orig_idx * 5, orig_idx * 5 + 5)
        ])

        if return_edits:
            return (PackedDecoderData(rnn_inputs, rnn_outputs, io_embed_indices,
                ref_code), edit_lists)
        else:
            return PackedDecoderData(rnn_inputs, rnn_outputs, io_embed_indices,
                ref_code)

    def compute_edit_ops_no_char(self, batch, code_seqs, ref_code):
        #print([self.vocab._rev_vocab[int(token)] for cd in code_seqs for token in cd if token > 0])
        edit_lists = []
        for batch_idx, item in enumerate(zip(batch,code_seqs)):
            # Removed the previously made sos token and end token
            code_sequence = list(np.array(item[1])[np.array(item[1])>-1]) #-1])[1:-1]
            # Double 
            ref_example_code_sequence = list(np.array(item[0])[np.array(item[0])>-1])
            edit_ops =  list(
                    edit.compute_edit_ops_no_stoi(ref_example_code_sequence,
                        code_sequence))
            dest_iter = itertools.chain(code_sequence)

            # Op = <s>, emb location, last token = <s>
            source_locs, ops, values = [list(x) for x in zip(*edit_ops)]
            #source_locs.append(len(ref_example_code_sequence))
            #ops = [0] + ops
            #values = [None] + values

            edit_list = []
            op_idx = 0
            for source_loc, op, value in zip(source_locs, ops, values):
                if op == 'keep':
                    op_idx = 2
                elif op == 'delete':
                    op_idx = 3
                elif op == 'insert':
                    op_idx = 4 + 2 * self.vocab.stoi(value)
                elif op == 'replace':
                    op_idx = 5 + 2 * self.vocab.stoi(value)
                elif isinstance(op, int):
                    op_idx = op
                else:
                    raise ValueError(op)

                # Set last token to UNK if operation is delete
                # XXX last_token should be 0 (<s>) at the beginning
                try:
                    if op_idx == 3:
                        last_token = 2
                    else:
                        last_token = next(dest_iter)
                except StopIteration:
                    raise Exception('dest_iter ended early')

                assert source_loc < ref_code.lengths[ref_code.sort_to_orig[batch_idx]]
                edit_list.append((
                    op_idx, ref_code.raw_index(batch_idx, source_loc),
                    last_token))
            stopped = False
            try:
                next(dest_iter)
            except StopIteration:
                stopped = True
            assert stopped

            # Op = </s>, emb location and last token are irrelevant
            #edit_list.append((1, None, None))
            edit_lists.append(edit_list)

        rnn_inputs = lists_to_packed_sequence(
                [lst[:-1] for lst in edit_lists], (3,), torch.LongTensor,
                lambda op_emb_pos_last_token, _, out:
                out.copy_(torch.LongTensor([*op_emb_pos_last_token])))
        rnn_outputs = lists_to_packed_sequence(
                [lst[1:] for lst in edit_lists], (1,), torch.LongTensor,
                lambda op_emb_pos_last_token, _, out:
                out.copy_(torch.LongTensor([op_emb_pos_last_token[0]])))

        io_embed_indices = torch.LongTensor([
            expanded_idx
            for b in rnn_inputs.ps.batch_sizes
            for orig_idx in rnn_inputs.orig_to_sort[:b]
            for expanded_idx in range(orig_idx * 5, orig_idx * 5 + 5)
        ])

        return PackedDecoderData(rnn_inputs, rnn_outputs, io_embed_indices,
                ref_code)

    def interleave_grids_events(self, batch):
        events_lists = [
            test['trace'].events
            for item in batch for test in item.ref_example.input_tests
        ]
        result = []
        for events_list in events_lists:
            get_from_events = []
            last_timestep = None
            for ev in events_list:
                if last_timestep != ev.timestep:
                    get_from_events.append(0)
                    last_timestep = ev.timestep
                get_from_events.append(1)
            # TODO: Devise better way to test if an event is an action
            if ev.cond_span is None and ev.success:
                # Trace ends with a grid, if last event is action and it is
                # successful
                get_from_events.append(0)
            result.append(get_from_events)
        return result

    def prepare_traces_grids(self, batch):
        grids_lists = [
            test['trace'].grids
            for item in batch for test in item.ref_example.input_tests
        ]

        last_grids = [set() for _ in grids_lists]
        def fill(grid, batch_idx, out):
            if isinstance(grid, dict):
                last_grid = last_grids[batch_idx]
                assert last_grid.isdisjoint(grid['plus'])
                assert last_grid >= grid['minus']
                last_grid.update(grid['plus'])
                last_grid.difference_update(grid['minus'])
            else:
                last_grid = last_grids[batch_idx] = set(grid)
            out.zero_()
            out.view(-1)[list(last_grid)] = 1
        ref_trace_grids = lists_to_packed_sequence(grids_lists, (15, 18, 18),
                torch.FloatTensor, fill)
        return ref_trace_grids

    def get_spans(self, batch, ref_code):
        spans = []
        for item in batch:
            spans_for_item = []
            for test in item.ref_example.input_tests:
                spans_for_trace = []
                for event in test['trace'].events:
                    spans_for_trace.append((event.timestep, event.span, event.cond_span))
                spans_for_item.append(spans_for_trace)
            spans.append(spans_for_item)
        return Spans(spans)

    def prepare_traces_events(self, batch, ref_code):
        # Split into action and cond events
        all_action_events = []
        all_cond_events = []
        interleave_indices = []
        for item in batch:
            for test in item.ref_example.input_tests:
                action_events, cond_events, interleave  = [], [], []
                for event in test['trace'].events:
                    # TODO: Devise better way to test if an event is an action
                    if event.cond_span is None:
                        action_events.append(event)
                        interleave.append(1)
                    else:
                        cond_events.append(event)
                        interleave.append(0)
                all_action_events.append(action_events)
                all_cond_events.append(cond_events)
                interleave_indices.append(interleave)

        packed_action_events = lists_to_packed_sequence(
                all_action_events,
                [2],
                torch.LongTensor,
                lambda ev, batch_idx, out: out.copy_(torch.LongTensor([
                    #{'if': 0, 'ifElse': 1, 'while': 2, 'repeat': 3}[ev.type],
                    ev.span[0], ev.success])))
        action_code_indices = None
        if ref_code:
            action_code_indices = Variable(torch.LongTensor(
                    ref_code.raw_index(
                        # TODO: Don't hardcode 5.
                        # TODO: May need to work with code replicated 5 times.
                        packed_action_events.orig_batch_indices() // 5,
                        packed_action_events.ps.data.data[:, 0].numpy())))

        packed_cond_events = lists_to_packed_sequence(
                all_cond_events,
                [6],
                torch.LongTensor,
                lambda ev, batch_idx, out: out.copy_(
                    torch.LongTensor([
                        ev.span[0], ev.span[1],
                        ev.cond_span[0], ev.cond_span[1],
                        int(ev.cond_value) if isinstance(ev.cond_value, (bool, np.bool))
                        else int(ev.cond_value + 2),
                        int(ev.success)])))
        cond_code_indices = None
        if ref_code:
            cond_code_indices = Variable(torch.LongTensor(
                    ref_code.raw_index(
                        # TODO: Don't hardcode 5.
                        np.expand_dims(
                            packed_cond_events.orig_batch_indices() // 5,
                            axis=1),
                        packed_cond_events.ps.data.data[:, :4].numpy())))

        return PackedTrace(
                packed_action_events, action_code_indices, packed_cond_events,
                cond_code_indices, interleave_indices)
