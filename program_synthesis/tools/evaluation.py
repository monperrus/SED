import argparse
import collections
import json
import sys
import time
import traceback
import random
import tqdm
import os

import sklearn
import numpy as np

from datasets import dataset
from datasets import executor
from datasets import stats


def _accuracy(stats):
    if stats['total'] == 0:
        return 0.0
    return float(stats['correct']) / stats['total']

def to_latex_table(value):
    print("$%.1f\%$" % (value * 100))


def to_latex_plot(values):
    print(''.join(["(%s, %.1f)" % (x, y * 100) for x, y in values]))


class EvalReport(object):

    def __init__(self, tag, show_info=True, report_path=None):
        self.tag = tag
        self.report = []
        self.stats = {'total': 0, 'correct': 0, 'syntax-error': 0, 'runtime-exception': 0}
        self.show_info = show_info
        self.report_path = report_path

    def add_example(self, example, code, st):
        self.report.append((example, code, st))
        self.stats['total'] += 1
        self.stats['correct'] += 1 if st['correct'] == st['total'] else 0
        self.stats['syntax-error'] += 1 if st['syntax-error'] == st['total'] else 0
        self.stats['runtime-exception'] += 1 if st['runtime-exception'] == st['total'] else 0

    def load(self, filename):
        with open(filename) as f:
            self.stats = json.loads(f.readline())
            self.report = []
            for line in f:
                info = json.loads(line)
                self.report.append((info['example'], info['code'], info['stats']))

    def to_html(self, filename):
        with open(filename, 'w') as f:
            f.write("<html><body>\n")
            f.write("<table>\n")
            for example, res, st in self.report:
                f.write("""<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>\n""" % (
                    example['text'], example['code_tree'],
                    res.code, stats
                ))
            f.write("</table>\n")
            f.write("</body></html>")

    def save(self, done=False):
        if self.report_path:
            report_path = self.report_path
        else:
            timestamp = int(time.time())
            report_path = 'reports/report-%s-%s.json' % (self.tag, timestamp)
        try:
            os.makedirs(os.path.dirname(report_path))
        except FileExistsError:
            pass
        with open(report_path, 'w') as f:
            f.write(json.dumps({**self.stats, 'done' : done}) + "\n")
            for example, res, st in self.report:
                f.write(json.dumps({
                    "stats": st,
                    "example": example.to_dict(),
                    "code": res.to_dict(),
                }) + "\n")

    def acc_by_func(self, func, stratify=True):
        total_by = collections.defaultdict(int)
        correct_by = collections.defaultdict(int)
        for example, res, st in self.report:
            res['language'] = example['language']
            keys = func(example, res)
            correct = st['total'] == st['correct']
            for key in keys:
                total_by[key] += 1
                correct_by[key] += 1 if correct else 0
        keys = sorted(total_by.keys())
        values = []
        for key in keys:
            if stratify:
                total = total_by[key]
            else:
                total = self.stats['total']
            print("%s: %.4f (%d / %d)" % (
                key, float(correct_by[key]) / total,
                correct_by[key], total
            ))
            values.append((key, float(correct_by[key]) / total))
        to_latex_plot(values)

    def show_example(self, example, res, st):
        # XXX: fix this.
        if isinstance(example, dict):
            example = dataset.CodeExample(
                text=example['text'], 
                schema=None,
                input_tests=None,
                code_tree=example['code_tree'],
                code_sequence=None,
                funcs=[],
                tests=[])
        if isinstance(res, dict):
            ir = collections.namedtuple('InferenceResult', ['code_tree', 'code_sequence', 'info'])
            res = ir(
                code_tree=res.get('code_tree', None), 
                code_sequence=res.get('code_sequence', None), 
                info=res.get('info', {}))
        print("Text:  %s" % ' '.join(example.text))
        if example.code_tree:
            print("Gold:  %s" % example.code_tree)
        if res.code_tree and not res.code_sequence:
            print("Res:   %s" % res.code_tree)
        elif res.code_sequence is not None:
            print("Res:   %s" % ' '.join(res.code_sequence))
        if self.show_info and res.info:
            print("Info:  %s" % res.info)
        print("Stats: %s" % st)

    def display(self, show_example=True):
        print("Total: %d, Correct: %d, Accuracy: %.4f, Syntax Errors: %d, Runtime Exceptions: %d" % (
            self.stats['total'], self.stats['correct'],
            _accuracy(self.stats),
            self.stats['syntax-error'], self.stats['runtime-exception']
        ))
        if show_example and self.report:
            example, res, st = self.report[-1]
            self.show_example(example, res, st)


def run_predict(dataset, inference, do_execute, inference_output_path, evaluate_on_all=False):
    """Runs inference of given model on eval set, and executes resulting code.

    Args:
        tag: str, tag of the run to save report.
        dataset: Dataset, iterable of CodeExample to evaluate on.
        inference: func, produces code for given CodeExamples.
        do_execute: func, runs given code with given arguments.
        show_info: Show specific example additional information.
    """
    assert inference_output_path is not None, "must provide path"
    assert not os.path.exists(inference_output_path), "must be a path that doesn't exist"
    assert os.path.isdir(os.path.dirname(inference_output_path)), "parent folder must exist"
    predictions = []
    success = total = 0
    pdataset = tqdm.tqdm(dataset)
    for batch in pdataset:
        results = inference(batch)
        for res, example in zip(results, batch.orig_examples):
            tests = []
            if evaluate_on_all:
                tests += list(example.input_tests)
            tests += list(example.tests)
            stats = executor.evaluate_code(res.code_sequence, example.schema.args, tests, do_execute)
            prediction = dict(
                output=res.info['candidates'][0],
                beams=res.info['candidates'],
                beams_correct=[executor.evaluate_code(hypothesis, example.schema.args, tests, do_execute) for hypothesis
                               in res.info['candidates']],
                is_correct=stats['correct'] == stats['total'],
                individual=stats['individual'],
                guid=example.guid,
            )
            if evaluate_on_all:
                prediction['passes_given_tests'] = all(stats['individual'][:len(example.input_tests)])
            predictions.append(prediction)
            success += stats['correct'] == stats['total']
            total += 1
            pdataset.set_description("Accuracy: {:.2f}%".format(success / total * 100))
    with open(inference_output_path, "w") as f:
        json.dump(predictions, f)

def limited(dataset, limit):
    count = 0
    for batch in dataset:
        if limit is not None and count >= limit:
            break
        count += batch.input_grids.shape[0]
        yield batch

def run_eval(tag, dataset, inference, do_execute, show_info=True,
        report_path=None, limit=None, evaluate_on_all=False):
    """Runs inference of given model on eval set, and executes resulting code.

    Args:
        tag: str, tag of the run to save report.
        dataset: Dataset, iterable of CodeExample to evaluate on.
        inference: func, produces code for given CodeExamples.
        do_execute: func, runs given code with given arguments.
        show_info: Show specific example additional information.
    """
    report = EvalReport(tag=tag, show_info=show_info, report_path=report_path)
    done = False
    try:
        for batch in limited(dataset, limit):
            start = time.time()
            results = inference(batch)
            for res, example in zip(results, batch.orig_examples):
                tests = []
                if evaluate_on_all:
                    tests += list(example.input_tests)
                tests += list(example.tests)
                stats = executor.evaluate_code(
                    res.code_tree if res.code_tree else res.code_sequence, example.schema.args, tests, do_execute)
                report.add_example(example, res, stats)
            print("[Eval] Elapsed time for %d examples: %f" %
                    (len(batch.orig_examples), time.time() - start))
            report.display()
        done = True
    finally:
        print("Stopped.")
        report.save(done)
        report.display()

def run_overfit_eval(dataset, inference, report_path=None, limit=None):
    """Runs inference of given model on eval set, and executes resulting code.

    Args:
        tag: str, tag of the run to save report.
        dataset: Dataset, iterable of CodeExample to evaluate on.
        inference: func, produces code for given CodeExamples.
        do_execute: func, runs given code with given arguments.
        show_info: Show specific example additional information.
    """
    true_labels = []
    pred_logits = []
    for batch in limited(dataset, limit):
        start = time.time()
        results = inference(batch)
        true_labels += [int(eg.ref_example.code_is_correct) for eg in batch.orig_examples]
        pred_logits += results.detach().cpu().numpy().tolist()
        confusion = sklearn.metrics.confusion_matrix(true_labels, np.array(pred_logits) > 0)
        accuracy = (confusion[0, 0] + confusion[1, 1]) / np.sum(confusion)
        print("Done with {} examples in {:.2f}s. Accuracy={:.2f} confusion={}".format(len(batch.orig_examples),
                                                                                      time.time() - start,
                                                                                      accuracy, confusion.tolist()))

    fpr, tpr, thresh = sklearn.metrics.roc_curve(true_labels, pred_logits)

    result = dict(
        accuracy=accuracy,
        confusion=confusion.tolist(),
        fpr=fpr.tolist(),
        tpr=tpr.tolist(),
        thresh=thresh.tolist(),
        done=True
    )

    if report_path:
        with open(report_path, "w") as f:
            json.dump(result, f)
    else:
        print(result)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='')
    parser.add_argument('--report', type=str, default=None)
    parser.add_argument('--errors', action='store_true', default=False)
    parser.add_argument('--accuracy', action='store_true', default=False)
    parser.add_argument('--show-depth', type=int, default=None)
    args, _ = parser.parse_known_args(sys.argv)

    report = EvalReport("", show_info=True)
    report.load(args.report)

    if args.accuracy:
        report.display(show_example=False)
        print("=== Accuracy by code tree depth ===")
        report.acc_by_func(lambda example, res: [stats.code_stat(example['code_tree'], example['language'])[0]])
        print("=== Accuracy by result tree depth ===")
        report.acc_by_func(lambda example, res: [stats.code_stat(res['code_tree'], res['language'])[0]])
        print("=== Accuracy by alls nodes ===")
        report.acc_by_func(lambda example, res: example['nodes'])
        print("=== Accuracy by top level metagen node ===")
        report.acc_by_func(lambda example, res: [example['nodes'][0]])
        print("=== Accuracy by trees searched ===")
        buckets = [1, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 65, 70, 75, 80, 85, 90, 95, 100]
        def to_buckets(idx):
            bs = []
            for i, b in enumerate(buckets):
                if idx <= b:
                    bs.append(b)
            return bs
        report.acc_by_func(lambda example, res: to_buckets(res['info']['trees_checked']), stratify=False)

    if args.errors:
        print(report.stats)
        errors = [(idx, e, r, s) for idx, (e, r, s) in enumerate(report.report)
                  if s['total'] != s['correct']]
        examples = random.sample(errors, min(5, len(errors)))
        for index, e, r, s in examples:
            print("\n=== Example #%d ===" % index)
            report.show_example(e, r, s)

    if args.show_depth:
        examples = [(idx, e, r, s) for idx, (e, r, s) in enumerate(report.report)
                    if stats.code_stat(e['code_tree'], e['language'])[0] == args.show_depth]
        examples = random.sample(examples, min(5, len(examples)))
        for index, e, r, s in examples:
            print("\n=== Example #%d ===" % index)
            report.show_example(e, r, s)
            print(e['tests'])
