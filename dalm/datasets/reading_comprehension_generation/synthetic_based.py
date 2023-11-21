import os
import argparse
from transformers import pipeline, Pipeline
import torch
import pickle
from dalm.datasets.reading_comprehension_generation.utils import list_dir, text_chunker, question_and_answer_extractor
import json
from datasets import Dataset
import logging

from typing import Dict, Any, Generator

logger = logging.getLogger(__name__)

PROMPT = (
    "There are 4 types of reading comprehension tasks. "
    "The point of reading comprehension tasks is to be assigned a text and questions to prompt answers so as to test conceptual and procedural knowledge "
    "present in the text. The four types of reading comprehension tasks are : 1. complete-the-sentence Q&A TASK "
    "2.true/false Q&A TASK (description: a sentence is posed and the user is asked to state the correctness of the statement) "
    "3. frame a sentence with domain specific keywords(these keywords are required to be present in the text) Q&A TASK "
    "4. Normal questions and answer Task (description: longform Q&A to test procedural and conceptual knowledge). "
    "An example of all four tasks given an example text is as follows: "
    "\n EXAMPLE TEXT: The insights into the mechanisms of memory consolidation during the sleep processes in human and animal brain led to other "
    "biologically inspired approaches. While declarative memories are in the classical picture consolidated by hippocampo-neocortical dialog during NREM phase "
    "of sleep, some types of procedural memories were suggested not to rely on the hippocampus and involve REM phase of the sleep. "
    "This inspired models where internal representations (memories) created by previous learning are spontaneously replayed during sleep-like periods in the network "
    "itself (i.e. without help of secondary network performed by generative replay approaches mentioned above).\n"
    "Question: [type: true/false] Is the following sentence true?  all types of procedural memories rely on the hippocampus\n"
    "Answer: False. The text clearly states there are some types of procedural memories not reliant on the hippocampus\n--------\n"
    "Question [type: complete-the-sentence] Complete the following sentence:  The insights into ____ in human and animal brain led to other _____ approaches\n"
    "Answer: The insights into the mechanisms of memory consolidation during the sleep processes in human and animal brain led to other biologically inspired approaches\n------\n"
    "Question [type 3 domain-keywords] Make a sentence with the following keywords 'hippocampo-neocortical', 'declarative' and 'NREM'\n"
    "Answer: declarative memories are in the classical picture consolidated by hippocampo-neocortical dialog during NREM phase of sleep\n-------\n"
    "Question [type: normal q&a] Some types of procedural memories were suggested not to rely on the hippocampus and involve REM phase of the sleep. What did this go on to inspire?\n"
    "Answer This inspired models where internal representations (memories) created by previous learning are spontaneously replayed during sleep-like periods in the network itself [END OF EXAMPLE]\n\n "
    "Similar to the above, could you craft 4 different reading comprehension tasks (make sure your output is a list of question answer pairs and each question is labelled QUESTION and answer is labelled ANSWER "
    "and there is one question and answer per task) based solely and completely focused on the following TEXT: "
)


def gen_prompt(text: str) -> Dict[str, Any]:
    prompt = PROMPT + text

    return [
        {
            "role": "system",
            "content": (
                "You are a helpful and meticulous instruction following question and answer making chatbot. "
                "Please refrain from acknowledgments, additions or niceties of any sort"
            ),
        },
        {"role": "user", "content": prompt},
    ]


def generate_synthetic_data(model_pipeline: Pipeline, text: str, generation_params: Dict[str, Any]) -> str:
    prompt = gen_prompt(text)
    prompt = model_pipeline.tokenizer.apply_chat_template(prompt, tokenize=False, add_generation_prompt=True)
    outputs = model_pipeline(prompt, **generation_params)

    return outputs[0]["generated_text"]


def generate_synthetic_dataset(
    model_name: str,
    input_directory: str,
    processed_files: [str],
    chunk: bool,
    context_length: int,
    generation_params: Dict[str, Any] = {
        "max_new_tokens": 600,
        "do_sample": True,
        "temperature": 0.7,
        "top_k": 5,
        "top_p": 0.95,
        "return_full_text": False,
    },
) -> Generator[str, str, str]:
    model_pipeline = pipeline("text-generation", model=model_name, torch_dtype=torch.bfloat16, device_map="auto")

    input_files = list_dir(input_directory)

    if chunk:
        tokenizer = model_pipeline.tokenizer
        tokens = tokenizer.apply_chat_template(gen_prompt(""), tokenize=False, add_generation_prompt=True)
        CONSTANT = len(tokenizer(tokens)["input_ids"])
        k = context_length - CONSTANT

    for file, text in input_files:
        if file in processed_files:
            continue

        if chunk:
            for chunk_ in text_chunker(text, tokenizer, k):
                gen_text = generate_synthetic_data(model_pipeline, chunk_, generation_params)
                yield file, chunk_, gen_text
        else:
            gen_text = generate_synthetic_data(model_pipeline, text, generation_params)
            yield file, text, gen_text

def parse_args():
    parser = argparse.ArgumentParser("Generate synthetic dataset for reading comprehension")
    parser.add_argument("--model_name", type=str, default="HuggingFaceH4/zephyr-7b-alpha")
    parser.add_argument("--input_directory", type=str, required=True, help="Directory containing the input files")
    parser.add_argument(
        "--output_directory",
        type=str,
        required=True,
        help="Directory to save the generated files (serves as intermediate step and for debugging purposes)",
    )
    parser.add_argument(
        "--state_file",
        type=str,
        required=False,
        default="rc_generation_state.pkl",
        help="File to save the state of the generation",
    )
    parser.add_argument("--context_length", type=int, default=4192, help="context length to calulcate the chunk size")
    parser.add_argument("--no_chunk", action="store_false")
    parser.add_argument(
        "--dataset_name", type=str, default="synthetic_rc_dataset", help="name of the dataset to be saved"
    )

    return parser.parse_args()

def main():
    args = parse_args()
    """
    Pipeline here includes chunking, generation and parsing of question and answer into a list of exchanges
    that can be used directly for training
    """

    if args.state_file:
        if os.path.exists(args.state_file):
            with open(args.state_file, "rb") as f:
                state = pickle.load(f)
        else:
            state = {"processed_files": []}
            pickle.dump(state, open(args.state_file, "wb"))

    if not os.path.exists(args.output_directory):
        os.makedirs(args.output_directory)

    files_missed = 0
    total_files = 0

    synth_dataset_generator = generate_synthetic_dataset(
        model_name=args.model_name,
        input_directory=args.input_directory,
        processed_files=state["processed_files"] if args.state_file else [],
        chunk=args.no_chunk,
        context_length=args.context_length,
    )

    for index, (filename, context, gen_text) in enumerate(synth_dataset_generator):
        state["processed_files"].append(filename)
        pickle.dump(state, open(args.state_file, "wb"))
        qanda = question_and_answer_extractor(gen_text, context)
        if qanda:
            output_file = f"gen_{index}.json"
            with open(os.path.join(args.output_directory, output_file), "w") as o:
                json.dump(qanda, o)
        else:
            logger.warning(f"No question and answer pairs found for {filename}")
            files_missed += 1
        total_files += 1

    unit = "files" if args.no_chunk else "chunks"

    logger.info(" Statistics ")
    logger.info(f"Total number of successfully extracted q&a {unit}: {total_files - files_missed}")
    logger.info(f"Total {unit} missed: {files_missed} out of {total_files}")

    in_memory_dataset = []
    for file in os.listdir(args.output_directory):
        with open(os.path.join(args.output_directory, file), "r") as f:
            in_memory_dataset.append({"messages": json.load(f)})

    dataset = Dataset.from_list(in_memory_dataset)
    dataset.save_to_disk(args.dataset_name)

    logger.info("Done generating synthetic dataset")


if __name__ == "__main__":
    main()
    

    
