__copyright__ = """MIT License

Copyright (c) 2024 - IBM Research

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE."""


from datetime import datetime
from pathlib import Path

import torch
from pydantic import BaseModel
from tqdm import tqdm

from mblm import MBLM, MBLMReturnType
from mblm.analysis.utils import load_model
from mblm.data.dataset.clevr import Clevr, ClevrOptionalArgs
from mblm.data.types import ModelMode
from mblm.utils.seed import seed_everything

DEVICE = "cuda"

# clevr has no labelled test set
DATASET_MODE = ModelMode.VALID


class ClevrModelGeneration(BaseModel):
    id_model: str
    sample_idx: int
    question: str
    question_type: str
    answer_gen: list[int]
    answer_truth: list[int]
    ce: float
    timestamp: str


def sample_clevr_by_question_type(clevr: Clevr, items_per_question: int) -> list[tuple[str, int]]:
    """
    Sample clevr indices by question type and return a list of (question_type,
    index) tuples.

    From the paper: "We categorize questions by question type, defined by the
    outermost function in the question's program"
    """
    seed_everything(8)
    counts = {q: list[int]() for q in Clevr.QUESTION_TYPES.keys()}
    for sample_i, sample in clevr.iter(shuffle=True, raw=True):
        q_type = sample["program"][-1]["function"]
        if len(q_list := counts[q_type]) < items_per_question:
            q_list.append(sample_i)
    sample_idxs_flat = [(q, i) for (q, q_idxs) in counts.items() for i in q_idxs]
    assert len(sample_idxs_flat) == items_per_question * len(Clevr.QUESTION_TYPES)
    return sample_idxs_flat


@torch.inference_mode()
@torch.autocast(device_type=DEVICE)
def sample_generation(
    model: MBLM,
    model_id: str,
    clevr: Clevr,
    sample_idx: int,
    question_type: str,
    output_file: Path,
) -> None:
    _, _, (question, image, answer, _) = clevr.get_sample_with_parts(sample_idx)

    # reconstruct the prompt
    prompt_qiq = torch.concat([question, image, question]).long().to(DEVICE)

    max_tokens_to_generate = len(answer)

    generated_qiqa = model.generate(
        prompt_qiq,
        temperature=1,
        num_tokens_to_generate=max_tokens_to_generate,
        enable_progress=False,
    )
    # feed the generated bytes back into the model (with additional
    # batch dim) to get the loss associated with this generation
    loss = model.forward(generated_qiqa.unsqueeze(0), return_type=MBLMReturnType.LOSS)

    # the generated bytes also contain the prompt, strip it
    generated_answer = generated_qiqa[len(prompt_qiq) :]
    raw_sample = clevr.get_sample_raw(sample_idx)
    output = ClevrModelGeneration(
        id_model=model_id,
        sample_idx=sample_idx,
        # not strictly needed - save the answer as a string for convenient
        # processing
        question=raw_sample["question"],
        # From the paper: "We categorize questions by question type, defined by
        # the outermost function in the question’s program"
        question_type=question_type,
        answer_gen=generated_answer.tolist(),
        answer_truth=answer.tolist(),
        ce=float(loss.item()),
        timestamp=str(datetime.now()),
    )

    with output_file.open("a") as f:
        f.write(output.model_dump_json() + "\n")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--clevr-dir",
        dest="clevr_dir",
        type=Path,
    )
    parser.add_argument(
        "--out-file",
        dest="output_file",
        type=Path,
    )
    parser.add_argument(
        "--model-dir",
        dest="model_dir",
        type=Path,
    )
    parser.add_argument(
        "-m",
        dest="model_id",
        action="append",
        type=str,
    )
    parser.add_argument(
        "-n",
        dest="num_samples_per_question",
        type=int,
    )

    args = parser.parse_args()
    output_file: Path = args.output_file
    model_dir: Path = args.model_dir
    num_samples_per_question: int = args.num_samples_per_question
    model_ids: list[str] = args.model_id
    clevr_dir: Path = args.clevr_dir

    for model_id in model_ids:
        print(f"Model {model_id}")
        model, model_config = load_model(model_id=model_id, model_dir=model_dir, device=DEVICE)
        model.eval()
        clevr_dataset_config = ClevrOptionalArgs.model_validate(model_config.io.dataset_args)
        clevr = Clevr(
            clevr_dir,
            mode=DATASET_MODE,
            seq_len=model_config.params.input_seq_len,
            pad_token_id=model_config.params.pad_token_id,
            optional_args=clevr_dataset_config,
            num_workers=1,
            worker_id=0,
        )
        sample_idxs = sample_clevr_by_question_type(clevr, num_samples_per_question)

        for question_type, sample_idx in (pbar := tqdm(sample_idxs)):
            pbar.set_description(f"Sample {sample_idx}")
            sample_generation(
                model=model,
                model_id=model_id,
                clevr=clevr,
                sample_idx=sample_idx,
                question_type=question_type,
                output_file=output_file,
            )