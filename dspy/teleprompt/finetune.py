import os
import time
import dsp
import tqdm
import random

import ujson
from datasets.fingerprint import Hasher
import dspy

# from dspy.primitives import Example

from .teleprompt import Teleprompter
from .bootstrap import BootstrapFewShot
# from .vanilla import LabeledFewShot

# from dspy.evaluate.evaluate import Evaluate


if os.environ.get('DSP_NOTEBOOK_CACHEDIR'):
    training_data_directory = os.path.join(os.environ.get('DSP_NOTEBOOK_CACHEDIR'), 'compiler')
    print(training_data_directory)
else:
    training_data_directory = 'local_cache/compiler'

if not os.path.exists(training_data_directory):
    os.makedirs(training_data_directory)


"""
TODO: Reduce and document the dependencies.

# !pip install evaluate
# !pip install tensorboardX
# !pip install transformers[torch]
# !pip install accelerate -U
# !pip install rouge_score


fewshot_teleprompter = BootstrapFewShot(metric=lambda gold, prediction, trace: gold.answer == prediction.answer,
                                        max_bootstrapped_demos=3, max_labeled_demos=16,
                                        teacher_settings=dict(lm=turbo))

fewshot = fewshot_teleprompter.compile(MyMultiHop(passages_per_hop=2), trainset=trainset)

"""


class BootstrapFinetune(Teleprompter):
    def __init__(self, metric=None, provider=None, teacher_settings={}, multitask=True, return_hf_model=True):
        self.metric = metric
        self.provider = provider
        self.teacher_settings = teacher_settings
        self.multitask = multitask
        self.return_hf_model = return_hf_model

        assert (not self.multitask) == self.return_hf_model

        metric = metric or (lambda *args: True)
        self.teleprompter = BootstrapFewShot(metric=metric,
                                             max_bootstrapped_demos=999999,
                                             max_labeled_demos=0,  # FIXME: TODO: Make this zero? or param, with default as 16 or 0?
                                             teacher_settings=teacher_settings)
        

    def compile(self, student, *, teacher=None, trainset, valset=None,
                target='t5-large', bsize=12, accumsteps=1, lr=5e-5, epochs=1, bf16=False, int8=False, peft=False):

        # It's usually better to supply a few-shot teacher, rather than uncompiled module (the student).
        if teacher is None:
            print("WARNING: Using a vanilla teacher. "
                  "Are you sure you want to use BootstrapFinetune without a compiled teacher?")


        teachers = teacher if isinstance(teacher, list) else [teacher]
        finetune_data = {}

        for teacher in teachers:
            # Dummy compilation to get bootstraps.
            compiled = self.teleprompter.compile(student, teacher=teacher, trainset=trainset)
            multitask = self.multitask
            for name, predictor in compiled.named_predictors():
                name_ = 'all' if multitask else name
                finetune_data[name_] = [] if name_ not in finetune_data else finetune_data[name_]

                for demo in predictor.demos:
                    demo = dict(demo)
                    completion = demo.pop(predictor.signature.fields[-1].output_variable)
                    prompt = predictor.signature.query(dsp.Example(demos=[], **demo)).strip()
                    if self.provider == "openai":
                        example = {
                            "messages": [
                                {"role": "system", "content": f"Answer questions with short factoid answers."},
                                {"role": "user", "content": prompt},
                                {"role": "assistant", "content": completion}
                            ]
                        }
                    elif self.provider == "hf":
                        example = dict(prompt=prompt, completion=completion)
                    finetune_data[name_].append(example)

        for name_ in finetune_data:
            random.Random(0).shuffle(finetune_data[name_])
            print(name_, len(finetune_data[name_]))


        #
        # Dump as files.
        # 
        finetune_paths = {}

        for name in finetune_data:
            data = finetune_data[name]
            hashed_name = name + '.' + Hasher.hash(data)
            output_path = os.path.join(training_data_directory, f'{hashed_name}.jsonl')
            print(output_path)

            with open(output_path, 'w') as f:
                for line in data:
                    f.write(ujson.dumps(line) + '\n')
            
            finetune_paths[name] = output_path
        

        #
        # Train!
        #
        import string
        compiler_config = {
            'save': ''.join(random.Random(time.time()).choices(string.ascii_uppercase + string.digits, k=13)), # https://stackoverflow.com/a/2257449/1493011
            'peft': peft,
            'fp16': False,
            'bf16': bf16,
            'int8': int8,
            'fid': False,
            'rationale': False,
            'batch_size': bsize,
            'epochs': epochs,
            'gradient_accumulation_steps': accumsteps, # 2,
            'lr': lr
        }

        from dsp.modules.finetuning import finetune_hf
        from dsp.modules.finetuning.finetune_open_ai import finetune_open_ai

        target = target
        finetune_models = {}

        for name in finetune_data:
            training_data_path = finetune_paths[name]
            if self.provider == "openai":
                best_fine_tuned_model = finetune_open_ai(training_data_path, target)
                finetune_models[name] = dsp.GPT3(model=best_fine_tuned_model)
            elif self.provider == "hf":
                compiler_config_ = dict(compiler_config)
                compiler_config_['save'] = compiler_config['save'] + '.' + name
                best_ckpt_path = finetune_hf(training_data_path, target, compiler_config_)
                if not self.return_hf_model:
                    finetune_models[name] = best_ckpt_path
                else:
                    finetune_models[name] = dsp.HFModel(model=target, checkpoint=best_ckpt_path) # best_ckpt_path

        #
        # Set the LMs to the finetuned ones, per module
        #
        compiled2 = compiled.reset_copy()
        if not self.return_hf_model:
            compiled2.checkpoint = finetune_models
            return compiled2
        assert len(compiled.named_predictors()) == len(compiled2.named_predictors())

        for (name, predictor), (name2, predictor2) in zip(compiled.named_predictors(), compiled2.named_predictors()):
            assert name == name2
            name = 'all' if multitask else name

            # TODO: FIXME: When we assign .lm, the Predict.forward will also set only_query=True.
            # This is correct for here but we may want to make it more explicitly restricted to finetuned models.
            print(f"Assigning the LM of predictor {name}.")

            predictor2.lm = finetune_models[name]
            assert predictor2.demos == []
        
        return compiled2
