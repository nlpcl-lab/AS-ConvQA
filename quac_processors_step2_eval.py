# Copyright 2020 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from ast import AsyncFunctionDef
import json
import os
import copy
import random
from functools import partial
from multiprocessing import Pool, cpu_count

import numpy as np
from tqdm import tqdm

from transformers import (
    AutoConfig,
    AutoTokenizer,
)

from transformers.file_utils import is_tf_available, is_torch_available
from transformers.tokenization_bert import whitespace_tokenize
from transformers.tokenization_utils_base import BatchEncoding, TruncationStrategy
from transformers.utils import logging
from transformers.data.processors.utils import DataProcessor

import argparse

# Store the tokenizers which insert 2 separators tokens
MULTI_SEP_TOKENS_TOKENIZERS_SET = {"roberta", "camembert", "bart", "mpnet"}

if is_torch_available():
    import torch
    from torch.utils.data import TensorDataset

logger = logging.get_logger(__name__)

def _is_whitespace(c):
    if c == " " or c == "\t" or c == "\r" or c == "\n" or ord(c) == 0x202F:
        return True
    return False

def _new_check_is_max_context(doc_spans, cur_span_index, position):
    """Check if this is the 'max context' doc span for the token."""
    # if len(doc_spans) == 1:
    # return True
    best_score = None
    best_span_index = None
    for (span_index, doc_span) in enumerate(doc_spans):
        end = doc_span["start"] + doc_span["length"] - 1
        if position < doc_span["start"]:
            continue
        if position > end:
            continue
        num_left_context = position - doc_span["start"]
        num_right_context = end - position
        score = min(num_left_context, num_right_context) + 0.01 * doc_span["length"]
        if best_score is None or score > best_score:
            best_score = score
            best_span_index = span_index

    return cur_span_index == best_span_index

def _improve_answer_span(doc_tokens, input_start, input_end, tokenizer, orig_answer_text):
    """Returns tokenized answer spans that better match the annotated answer."""
    tok_answer_text = " ".join(tokenizer.tokenize(orig_answer_text))

    for new_start in range(input_start, input_end + 1):
        for new_end in range(input_end, new_start - 1, -1):
            text_span = " ".join(doc_tokens[new_start : (new_end + 1)])
            if text_span == tok_answer_text:
                return (new_start, new_end)

    return (input_start, input_end)


class QuacProcessor(DataProcessor):
    """
    Processor for the QuAC data set.
    """

    train_file = "train.json"
    dev_file = "dev.json"

    def __init__(self, tokenizer, threshold, conf_or_uncer):
        self.tokenizer_name = tokenizer.__class__.__name__
        self.sep_token = tokenizer.sep_token
        self.threshold = threshold
        self.conf_or_uncer = conf_or_uncer

    def _get_example_from_tensor_dict(self, tensor_dict, evaluate=False):
        if not evaluate:
            answer = tensor_dict["answers"]["text"][0].numpy().decode("utf-8")
            answer_start = tensor_dict["answers"]["answer_start"][0].numpy()
            answers = []
        else:
            answers = [
                {"answer_start": start.numpy(), "text": text.numpy().decode("utf-8")}
                for start, text in zip(tensor_dict["answers"]["answer_start"], tensor_dict["answers"]["text"])
            ]

            answer = None
            answer_start = None

        return QuacExample(
            qas_id=tensor_dict["id"].numpy().decode("utf-8"),
            question_text=tensor_dict["question"].numpy().decode("utf-8"),
            context_text=tensor_dict["context"].numpy().decode("utf-8"),
            answer_text=answer,
            rewrite_text=tensor_dict["rewrite"].numpy().decode("utf-8"),
            start_position_character=answer_start,
            title=tensor_dict["title"].numpy().decode("utf-8"),
            answers=answers,
        )

    def get_examples_from_dataset(self, dataset, evaluate=False):
        """
        Creates a list of :class:`QuacExample` using a TFDS dataset.

        """

        if evaluate:
            dataset = dataset["validation"]
        else:
            dataset = dataset["train"]

        examples = []
        for tensor_dict in tqdm(dataset):
            examples.append(self._get_example_from_tensor_dict(tensor_dict, evaluate=evaluate))

        return examples

    def get_train_examples(self, data_dir, filename=None, excord=False):
        """
        Returns the training examples from the data directory.

        Args:
            data_dir: Directory containing the data files used for training and evaluating.
            filename: None by default, specify this if the training file has a different name than the original one
                which is `train.json`.

        """
        if data_dir is None:
            data_dir = ""

        if self.train_file is None:
            raise ValueError("--train_file should be given.")

        with open(
            os.path.join(data_dir, self.train_file if filename is None else filename), "r", encoding="utf-8"
        ) as reader:
            input_data = json.load(reader)["data"]
        return self._create_examples(input_data, "train", excord=excord)

    def get_dev_examples(self, data_dir, filename=None):
        """
        Returns the evaluation example from the data directory.

        Args:
            data_dir: Directory containing the data files used for training and evaluating.
            filename: None by default, specify this if the evaluation file has a different name than the original one
                which is `dev.json`.
        """
        if data_dir is None:
            data_dir = ""

        if self.dev_file is None:
            raise ValueError("--dev_file should be given.")

        with open(
            os.path.join(data_dir, self.dev_file if filename is None else filename), "r", encoding="utf-8"
        ) as reader:
            input_data = json.load(reader)["data"]
        return self._create_examples(input_data, "dev")

    def _calcaulte_qas_in_examples_number(self, input_data):
        for entry in tqdm(input_data):
            for paragraph in entry["paragraphs"]:
                qas = paragraph["qas"]
        
        return len(qas)

    def _create_examples(self, input_data, set_type, excord=False):
        is_training = set_type == "train"
        examples = []
        for entry in tqdm(input_data):
            title = entry["title"]
            for paragraph in entry["paragraphs"]:
                context_text = paragraph["context"]
                for qa_idx, qa in enumerate(paragraph["qas"]):
                    qas_id = qa["id"]
                    # question_text = self._concat_history(paragraph["qas"], qa_idx, 
                    #                                     max_history=1)

                    # question_text = self._concat_history(paragraph["qas"], predicted_previous_qas, qa_idx, 
                    #                                      max_history=1)
                    question_text = None
                    rewrite_text = None
                    # if is_training and excord:
                    #     rewrite_text = self._concat_history(paragraph["qas"], qa_idx, 
                    #                                         max_history=1, question_key='rewrite')
                    start_position_character = None
                    answer_text = None
                    answers = []

                    is_impossible = 1 if qa['orig_answer']['text'] == 'CANNOTANSWER' else 0
                    if not is_impossible:
                        if is_training:
                            answer = qa["answers"][0]
                            answer_text = answer["text"]
                            start_position_character = answer["answer_start"]
                        else:
                            answers = qa["answers"]
                            #TODO TODO
                            answer_text = qa['orig_answer']['text']
                            start_position_character = qa['orig_answer']['answer_start']

                    example = QuacExample(
                        qas_id=qas_id,
                        question_text=question_text,
                        context_text=context_text,
                        answer_text=answer_text,
                        rewrite_text=rewrite_text,
                        start_position_character=start_position_character,
                        title=title,
                        is_impossible=is_impossible,
                        answers=answers,
                    )
                    examples.append(example)
        return examples

    def _concat_history(self, qas, predicted_previous_qas, qa_idx, max_history, question_key="question"):
        question_text = ""
        sep_token = self.sep_token
        if self.tokenizer_name in ["RobertaTokenizer", "RobertaTokenizerFast"]:
            sep_token += self.sep_token
        
        for i in range(max_history, 0, -1):          
            if qa_idx - i >= 0:
                # question_text += sep_token + \
                #                 qas[qa_idx - i][question_key] + \
                #                 sep_token + \
                #                 qas[qa_idx - i]['orig_answer']['text']
                
                if self.conf_or_uncer == 'conf':
                    if predicted_previous_qas[qas[qa_idx - i]['id']]['confidence'] > self.threshold:
                        question_text += sep_token + \
                                        qas[qa_idx - i][question_key] + \
                                        sep_token + \
                                        predicted_previous_qas[qas[qa_idx - i]['id']]['predicted_answer_text']
                    else:
                        question_text += sep_token + \
                                        qas[qa_idx - i][question_key] 
                                
                elif self.conf_or_uncer == 'uncer':
                    if predicted_previous_qas[qas[qa_idx - i]['id']]['uncertainty'] < self.threshold:
                        question_text += sep_token + \
                                        qas[qa_idx - i][question_key] + \
                                        sep_token + \
                                        predicted_previous_qas[qas[qa_idx - i]['id']]['predicted_answer_text']
                                        #qas[qa_idx - i]['orig_answer']['text'] #이거 하고 thres 1했을때 원래랑 같아야함
                                        
                    else:
                        question_text += sep_token + \
                                        qas[qa_idx - i][question_key]

                elif self.conf_or_uncer == 'conf_uncer':
                    confidence = predicted_previous_qas[qas[qa_idx - i]['id']]['confidence']
                    uncertainty = predicted_previous_qas[qas[qa_idx - i]['id']]['uncertainty']

                    confidence_uncertainty = (confidence + (1 - uncertainty)) / 2
                    
                    if confidence_uncertainty > self.threshold:
                        question_text += sep_token + \
                                        qas[qa_idx - i][question_key] + \
                                        sep_token + \
                                        predicted_previous_qas[qas[qa_idx - i]['id']]['predicted_answer_text']
                    else:
                        question_text += sep_token + \
                                        qas[qa_idx - i][question_key] 

        question_text = qas[qa_idx][question_key] + question_text
        #import pdb; pdb.set_trace()
        if qa_idx - i >= 0:
            logger.info("======================")
            logger.info("qa_idx: %d", qa_idx)
            logger.info("previous qa: {}".format(predicted_previous_qas[qas[qa_idx - i]['id']]))
            logger.info("conf_or_uncer: {}".format(self.conf_or_uncer))
            logger.info("threshold: {}".format(self.threshold))
            
            if self.conf_or_uncer == 'conf':
                logger.info("confidence: {}".format(predicted_previous_qas[qas[qa_idx - i]['id']]['confidence']))
            
            elif self.conf_or_uncer == 'uncer':
                logger.info("uncertainty: {}".format(predicted_previous_qas[qas[qa_idx - i]['id']]['uncertainty']))
            
            elif self.conf_or_uncer == 'conf_uncer':
                logger.info("confidence: {}".format(predicted_previous_qas[qas[qa_idx - i]['id']]['confidence']))
                logger.info("uncertainty: {}".format(predicted_previous_qas[qas[qa_idx - i]['id']]['uncertainty']))
                logger.info("confidence_uncertainty: %f", confidence_uncertainty)
                
            logger.info("question_text: {}".format(question_text))
            logger.info("answers: {}".format(qas[qa_idx]['answers']))
            #import pdb; pdb.set_trace()
        #import pdb; pdb.set_trace()
        return question_text

class QuacExample:
    """
    A single training/test example for the QuAC dataset, as loaded from disk.

    Args:
        qas_id: The example's unique identifier
        question_text: The question string
        context_text: The context string
        answer_text: The answer string
        start_position_character: The character position of the start of the answer
        title: The title of the example
        answers: None by default, this is used during evaluation. Holds answers as well as their start positions.
        is_impossible: False by default, set to True if the example has no possible answer.
    """

    def __init__(
        self,
        qas_id,
        question_text,
        context_text,
        answer_text,
        rewrite_text,
        start_position_character,
        title,
        answers=[],
        is_impossible=False,
    ):
        self.qas_id = qas_id
        self.question_text = question_text
        self.context_text = context_text
        self.answer_text = answer_text
        self.rewrite_text = rewrite_text
        self.title = title
        self.is_impossible = is_impossible
        self.answers = answers

        self.start_position, self.end_position = 0, 0

        doc_tokens = []
        char_to_word_offset = []
        prev_is_whitespace = True

        # Split on whitespace so that different tokens may be attributed to their original position.
        for c in self.context_text:
            if _is_whitespace(c):
                prev_is_whitespace = True
            else:
                if prev_is_whitespace:
                    doc_tokens.append(c)
                else:
                    doc_tokens[-1] += c
                prev_is_whitespace = False
            char_to_word_offset.append(len(doc_tokens) - 1)

        self.doc_tokens = doc_tokens
        self.char_to_word_offset = char_to_word_offset

        # Start and end positions only has a value during evaluation.
        if start_position_character is not None and not is_impossible:
            self.start_position = char_to_word_offset[start_position_character]
            self.end_position = char_to_word_offset[
                min(start_position_character + len(answer_text) - 1, len(char_to_word_offset) - 1)
            ]


def quac_convert_example_to_features(
    example, max_seq_length, doc_stride, max_query_length, padding_strategy, is_training, 
    excord=False,
):
    features = []

    #if is_training and not example.is_impossible: #TODO TODO
    if not example.is_impossible:
        # Get start and end position
        start_position = example.start_position
        end_position = example.end_position

        # If the answer cannot be found in the text, then skip this example.
        actual_text = " ".join(example.doc_tokens[start_position : (end_position + 1)])
        cleaned_answer_text = " ".join(whitespace_tokenize(example.answer_text))
        if actual_text.find(cleaned_answer_text) == -1:
            logger.warning("Could not find answer: '%s' vs. '%s'", actual_text, cleaned_answer_text)
        #     return []

    tok_to_orig_index = []
    orig_to_tok_index = []
    all_doc_tokens = []
    for (i, token) in enumerate(example.doc_tokens):
        orig_to_tok_index.append(len(all_doc_tokens))
        if tokenizer.__class__.__name__ in [
            "RobertaTokenizer",
            "LongformerTokenizer",
            "BartTokenizer",
            "RobertaTokenizerFast",
            "LongformerTokenizerFast",
            "BartTokenizerFast",
        ]:
            sub_tokens = tokenizer.tokenize(token, add_prefix_space=True)
        else:
            sub_tokens = tokenizer.tokenize(token)
        for sub_token in sub_tokens:
            tok_to_orig_index.append(i)
            all_doc_tokens.append(sub_token)

    #if is_training and not example.is_impossible: #TODO TODO
    if not example.is_impossible:
        tok_start_position = orig_to_tok_index[example.start_position]
        if example.end_position < len(example.doc_tokens) - 1:
            tok_end_position = orig_to_tok_index[example.end_position + 1] - 1
        else:
            tok_end_position = len(all_doc_tokens) - 1

        (tok_start_position, tok_end_position) = _improve_answer_span(
            all_doc_tokens, tok_start_position, tok_end_position, tokenizer, example.answer_text
        )

    spans = []

    truncated_query = tokenizer.encode(
        example.question_text, add_special_tokens=False, truncation=True, max_length=max_query_length
    )
    if is_training and excord:
        truncated_rewrite = tokenizer.encode(
            example.rewrite_text, add_special_tokens=False, truncation=True, max_length=max_query_length
        )
        encoded_ph = tokenizer.convert_tokens_to_ids(tokenizer.placeholder_token)
        
        if len(truncated_rewrite) > len(truncated_query):
            truncated_query   += [encoded_ph] * (len(truncated_rewrite) - len(truncated_query))
        else:
            truncated_rewrite += [encoded_ph] * (len(truncated_query) - len(truncated_rewrite))
        assert len(truncated_query) == len(truncated_rewrite)
    
    # Tokenizers who insert 2 SEP tokens in-between <context> & <question> need to have special handling
    # in the way they compute mask of added tokens.
    tokenizer_type = type(tokenizer).__name__.replace("Tokenizer", "").lower()
    sequence_added_tokens = (
        tokenizer.model_max_length - tokenizer.max_len_single_sentence + 1
        if tokenizer_type in MULTI_SEP_TOKENS_TOKENIZERS_SET
        else tokenizer.model_max_length - tokenizer.max_len_single_sentence
    )
    sequence_pair_added_tokens = tokenizer.model_max_length - tokenizer.max_len_sentences_pair

    span_doc_tokens = all_doc_tokens
    while len(spans) * doc_stride < len(all_doc_tokens):

        # Define the side we want to truncate / pad and the text/pair sorting
        if tokenizer.padding_side == "right":
            texts = truncated_query
            pairs = span_doc_tokens
            truncation = TruncationStrategy.ONLY_SECOND.value
        else:
            texts = span_doc_tokens
            pairs = truncated_query
            truncation = TruncationStrategy.ONLY_FIRST.value

        encoded_dict = tokenizer.encode_plus(  # TODO(thom) update this logic
            texts,
            pairs,
            truncation=truncation,
            padding=padding_strategy,
            max_length=max_seq_length,
            return_overflowing_tokens=True,
            stride=max_seq_length - doc_stride - len(truncated_query) - sequence_pair_added_tokens,
            return_token_type_ids=True,
        )
        
        paragraph_len = min(
            len(all_doc_tokens) - len(spans) * doc_stride,
            max_seq_length - len(truncated_query) - sequence_pair_added_tokens,
        )

        if tokenizer.pad_token_id in encoded_dict["input_ids"]:
            if tokenizer.padding_side == "right":
                non_padded_ids = encoded_dict["input_ids"][: encoded_dict["input_ids"].index(tokenizer.pad_token_id)]
            else:
                last_padding_id_position = (
                    len(encoded_dict["input_ids"]) - 1 - encoded_dict["input_ids"][::-1].index(tokenizer.pad_token_id)
                )
                non_padded_ids = encoded_dict["input_ids"][last_padding_id_position + 1 :]
        else:
            non_padded_ids = encoded_dict["input_ids"]

        tokens = tokenizer.convert_ids_to_tokens(non_padded_ids)

        token_to_orig_map = {}
        for i in range(paragraph_len):
            index = len(truncated_query) + sequence_added_tokens + i if tokenizer.padding_side == "right" else i
            token_to_orig_map[index] = tok_to_orig_index[len(spans) * doc_stride + i]
        
        encoded_dict["input_ids_rewrite"] = None
        if is_training and excord:
            input_ids_rewrite = copy.deepcopy(encoded_dict["input_ids"])
            assert input_ids_rewrite[1: len(truncated_query) + 1] == truncated_query

            input_ids_rewrite[1: len(truncated_query) + 1] = truncated_rewrite
            encoded_dict["input_ids_rewrite"] = input_ids_rewrite
            
        encoded_dict["paragraph_len"] = paragraph_len
        encoded_dict["tokens"] = tokens
        encoded_dict["token_to_orig_map"] = token_to_orig_map
        encoded_dict["truncated_query_with_special_tokens_length"] = len(truncated_query) + sequence_added_tokens
        encoded_dict["token_is_max_context"] = {}
        encoded_dict["start"] = len(spans) * doc_stride
        encoded_dict["length"] = paragraph_len

        spans.append(encoded_dict)

        if "overflowing_tokens" not in encoded_dict or (
            "overflowing_tokens" in encoded_dict and len(encoded_dict["overflowing_tokens"]) == 0
        ):
            break
        span_doc_tokens = encoded_dict["overflowing_tokens"]

    for doc_span_index in range(len(spans)):
        for j in range(spans[doc_span_index]["paragraph_len"]):
            is_max_context = _new_check_is_max_context(spans, doc_span_index, doc_span_index * doc_stride + j)
            index = (
                j
                if tokenizer.padding_side == "left"
                else spans[doc_span_index]["truncated_query_with_special_tokens_length"] + j
            )
            spans[doc_span_index]["token_is_max_context"][index] = is_max_context

    for span in spans:
        # Identify the position of the CLS token
        cls_index = span["input_ids"].index(tokenizer.cls_token_id)

        # p_mask: mask with 1 for token than cannot be in the answer (0 for token which can be in an answer)
        # Original TF implem also keep the classification token (set to 0)
        p_mask = np.ones_like(span["token_type_ids"])
        if tokenizer.padding_side == "right":
            p_mask[len(truncated_query) + sequence_added_tokens :] = 0
        else:
            p_mask[-len(span["tokens"]) : -(len(truncated_query) + sequence_added_tokens)] = 0

        pad_token_indices = np.where(span["input_ids"] == tokenizer.pad_token_id)
        special_token_indices = np.asarray(
            tokenizer.get_special_tokens_mask(span["input_ids"], already_has_special_tokens=True)
        ).nonzero()

        p_mask[pad_token_indices] = 1
        p_mask[special_token_indices] = 1

        # Set the cls index to 0: the CLS index can be used for impossible answers
        p_mask[cls_index] = 0

        span_is_impossible = example.is_impossible
        start_position = 0
        end_position = 0
        #if is_training and not span_is_impossible: #TODO TODO
        if not span_is_impossible:
            # For training, if our document chunk does not contain an annotation
            # we throw it out, since there is nothing to predict.
            doc_start = span["start"]
            doc_end = span["start"] + span["length"] - 1
            out_of_span = False

            if not (tok_start_position >= doc_start and tok_end_position <= doc_end):
                out_of_span = True

            if out_of_span:
                start_position = cls_index
                end_position = cls_index
                span_is_impossible = True
            else:
                if tokenizer.padding_side == "left":
                    doc_offset = 0
                else:
                    doc_offset = len(truncated_query) + sequence_added_tokens

                start_position = tok_start_position - doc_start + doc_offset
                end_position = tok_end_position - doc_start + doc_offset
                        
        features.append(
            QuacFeatures(
                span["input_ids"],
                span["attention_mask"],
                span["token_type_ids"],
                cls_index,
                p_mask.tolist(),
                example_index=0,  # Can not set unique_id and example_index here. They will be set after multiple processing.
                unique_id=0,
                paragraph_len=span["paragraph_len"],
                token_is_max_context=span["token_is_max_context"],
                tokens=span["tokens"],
                token_to_orig_map=span["token_to_orig_map"],
                start_position=start_position,
                end_position=end_position,
                is_impossible=span_is_impossible,
                qas_id=example.qas_id,
                input_ids_rewrite=span["input_ids_rewrite"],
                query_end=span["truncated_query_with_special_tokens_length"]
            )
        )
    return features

def quac_convert_example_to_features_init(tokenizer_for_convert):
    global tokenizer
    tokenizer = tokenizer_for_convert

def quac_convert_examples_to_features(
    examples,
    tokenizer,
    max_seq_length,
    doc_stride,
    max_query_length,
    is_training,
    padding_strategy="max_length",
    return_dataset=False,
    threads=1,
    tqdm_enabled=True,
    excord=False,
):
    """
    Converts a list of examples into a list of features that can be directly given as input to a model. It is
    model-dependant and takes advantage of many of the tokenizer's features to create the model's inputs.

    Args:
        examples: list of :class:`QuacExample`
        tokenizer: an instance of a child of :class:`~transformers.PreTrainedTokenizer`
        max_seq_length: The maximum sequence length of the inputs.
        doc_stride: The stride used when the context is too large and is split across several features.
        max_query_length: The maximum length of the query.
        is_training: whether to create features for model evaluation or model training.
        padding_strategy: Default to "max_length". Which padding strategy to use
        return_dataset: Default False. Either 'pt' or 'tf'.
            if 'pt': returns a torch.data.TensorDataset, if 'tf': returns a tf.data.Dataset
        threads: multiple processing threads.


    Returns:
        list of :class:`QuacExample`
    """
    # Defining helper methods
    random.seed(42)
    features = []

    threads = min(threads, cpu_count())
    with Pool(threads, initializer=quac_convert_example_to_features_init, initargs=(tokenizer,)) as p:
        annotate_ = partial(
            quac_convert_example_to_features,
            max_seq_length=max_seq_length,
            doc_stride=doc_stride,
            max_query_length=max_query_length,
            padding_strategy=padding_strategy,
            is_training=is_training,
            excord=excord,
        )
        features = list(
            tqdm(
                p.imap(annotate_, examples, chunksize=32),
                total=len(examples),
                desc="convert quac examples to features",
                disable=not tqdm_enabled,
            )
        )
    
    #print('features: '+str(features))
    # new_features = []
    # unique_id = 1000000000
    # example_index = 0
    # for example_features in tqdm(
    #     features, total=len(features), desc="add example index and unique id", disable=not tqdm_enabled
    # ):
    #     if not example_features:
    #         continue
    #     for example_feature in example_features:
    #         example_feature.example_index = example_index
    #         example_feature.unique_id = unique_id
    #         new_features.append(example_feature)
    #         unique_id += 1
    #     example_index += 1
    # features = new_features
    # #print(features)
    # del new_features
    features = features[0]
    if return_dataset == "pt":
        if not is_torch_available():
            raise RuntimeError("PyTorch must be installed to return a PyTorch dataset.")

        # Convert to Tensors and build dataset
        all_input_ids = torch.tensor([f.input_ids for f in features], dtype=torch.long)
        all_attention_masks = torch.tensor([f.attention_mask for f in features], dtype=torch.long)
        all_token_type_ids = torch.tensor([f.token_type_ids for f in features], dtype=torch.long)
        all_cls_index = torch.tensor([f.cls_index for f in features], dtype=torch.long)
        all_p_mask = torch.tensor([f.p_mask for f in features], dtype=torch.float)
        all_is_impossible = torch.tensor([f.is_impossible for f in features], dtype=torch.float)
        #(TODO) added
        #all_query_end = torch.tensor([f.query_end for f in features], dtype=torch.long)
        #TODO TODO
        all_start_positions = torch.tensor([f.start_position for f in features], dtype=torch.long)
        all_end_positions = torch.tensor([f.end_position for f in features], dtype=torch.long)
        
        if not is_training:
            all_feature_index = torch.arange(all_input_ids.size(0), dtype=torch.long)
            #print(all_feature_index)
            dataset = TensorDataset(
                all_input_ids,          # 0
                all_attention_masks,    # 1
                all_token_type_ids,     # 2
                all_feature_index,      # 3
                all_cls_index,          # 4
                all_p_mask,             # 5
                all_start_positions,    # 6 #TODO TODO
                all_end_positions       # 7 #TODO TODO
            )
        else:
            #all_start_positions = torch.tensor([f.start_position for f in features], dtype=torch.long) #TODO TODO
            #all_end_positions = torch.tensor([f.end_position for f in features], dtype=torch.long) #TODO TODO
            #(TODO) added
            #all_query_end = torch.tensor([f.query_end for f in features], dtype=torch.long)
            
            if excord:
                all_input_ids_rewrite = torch.tensor([f.input_ids_rewrite for f in features], dtype=torch.long)
                all_query_end         = torch.tensor([f.query_end for f in features], dtype=torch.long)

                dataset = TensorDataset(
                    all_input_ids,         # 0
                    all_attention_masks,   # 1
                    all_token_type_ids,    # 2
                    all_start_positions,   # 3
                    all_end_positions,     # 4
                    all_cls_index,         # 5
                    all_p_mask,            # 6
                    all_is_impossible,     # 7
                    all_input_ids_rewrite, # 8
                    all_query_end,         # 9
                )
            else:
                dataset = TensorDataset(
                    all_input_ids,          # 0
                    all_attention_masks,    # 1
                    all_token_type_ids,     # 2
                    all_start_positions,    # 3
                    all_end_positions,      # 4
                    all_cls_index,          # 5
                    all_p_mask,             # 6
                    all_is_impossible,      # 7
                )

        return features, dataset
    elif return_dataset == "tf":
        if not is_tf_available():
            raise RuntimeError("TensorFlow must be installed to return a TensorFlow dataset.")

        def gen():
            for i, ex in enumerate(features):
                if ex.token_type_ids is None:
                    yield (
                        {
                            "input_ids": ex.input_ids,
                            "attention_mask": ex.attention_mask,
                            "feature_index": i,
                            "qas_id": ex.qas_id,
                        },
                        {
                            "start_positions": ex.start_position,
                            "end_positions": ex.end_position,
                            "cls_index": ex.cls_index,
                            "p_mask": ex.p_mask,
                            "is_impossible": ex.is_impossible,
                        },
                    )
                else:
                    yield (
                        {
                            "input_ids": ex.input_ids,
                            "attention_mask": ex.attention_mask,
                            "token_type_ids": ex.token_type_ids,
                            "feature_index": i,
                            "qas_id": ex.qas_id,
                        },
                        {
                            "start_positions": ex.start_position,
                            "end_positions": ex.end_position,
                            "cls_index": ex.cls_index,
                            "p_mask": ex.p_mask,
                            "is_impossible": ex.is_impossible,
                        },
                    )

        # Why have we split the batch into a tuple? PyTorch just has a list of tensors.
        if "token_type_ids" in tokenizer.model_input_names:
            train_types = (
                {
                    "input_ids": tf.int32,
                    "attention_mask": tf.int32,
                    "token_type_ids": tf.int32,
                    "feature_index": tf.int64,
                    "qas_id": tf.string,
                },
                {
                    "start_positions": tf.int64,
                    "end_positions": tf.int64,
                    "cls_index": tf.int64,
                    "p_mask": tf.int32,
                    "is_impossible": tf.int32,
                },
            )

            train_shapes = (
                {
                    "input_ids": tf.TensorShape([None]),
                    "attention_mask": tf.TensorShape([None]),
                    "token_type_ids": tf.TensorShape([None]),
                    "feature_index": tf.TensorShape([]),
                    "qas_id": tf.TensorShape([]),
                },
                {
                    "start_positions": tf.TensorShape([]),
                    "end_positions": tf.TensorShape([]),
                    "cls_index": tf.TensorShape([]),
                    "p_mask": tf.TensorShape([None]),
                    "is_impossible": tf.TensorShape([]),
                },
            )
        else:
            train_types = (
                {"input_ids": tf.int32, "attention_mask": tf.int32, "feature_index": tf.int64, "qas_id": tf.string},
                {
                    "start_positions": tf.int64,
                    "end_positions": tf.int64,
                    "cls_index": tf.int64,
                    "p_mask": tf.int32,
                    "is_impossible": tf.int32,
                },
            )

            train_shapes = (
                {
                    "input_ids": tf.TensorShape([None]),
                    "attention_mask": tf.TensorShape([None]),
                    "feature_index": tf.TensorShape([]),
                    "qas_id": tf.TensorShape([]),
                },
                {
                    "start_positions": tf.TensorShape([]),
                    "end_positions": tf.TensorShape([]),
                    "cls_index": tf.TensorShape([]),
                    "p_mask": tf.TensorShape([None]),
                    "is_impossible": tf.TensorShape([]),
                },
            )

        return tf.data.Dataset.from_generator(gen, train_types, train_shapes)
    else:
        return features

class QuacFeatures:
    """
    Single quac example features to be fed to a model. Those features are model-specific and can be crafted from
    :class:`QuacExample` using the
    :method:`quac_convert_examples_to_features` method.

    Args:
        input_ids: Indices of input sequence tokens in the vocabulary.
        attention_mask: Mask to avoid performing attention on padding token indices.
        token_type_ids: Segment token indices to indicate first and second portions of the inputs.
        cls_index: the index of the CLS token.
        p_mask: Mask identifying tokens that can be answers vs. tokens that cannot.
            Mask with 1 for tokens than cannot be in the answer and 0 for token that can be in an answer
        example_index: the index of the example
        unique_id: The unique Feature identifier
        paragraph_len: The length of the context
        token_is_max_context: List of booleans identifying which tokens have their maximum context in this feature object.
            If a token does not have their maximum context in this feature object, it means that another feature object
            has more information related to that token and should be prioritized over this feature for that token.
        tokens: list of tokens corresponding to the input ids
        token_to_orig_map: mapping between the tokens and the original text, needed in order to identify the answer.
        start_position: start of the answer token index
        end_position: end of the answer token index
        encoding: optionally store the BatchEncoding with the fast-tokenizer alignement methods.
    """

    def __init__(
        self,
        input_ids,
        attention_mask,
        token_type_ids,
        cls_index,
        p_mask,
        example_index,
        unique_id,
        paragraph_len,
        token_is_max_context,
        tokens,
        token_to_orig_map,
        start_position,
        end_position,
        is_impossible,
        qas_id: str = None,
        encoding: BatchEncoding = None,
        input_ids_rewrite = None,
        query_end = None,
    ):
        self.input_ids = input_ids
        self.input_ids_rewrite = input_ids_rewrite
        self.attention_mask = attention_mask
        self.token_type_ids = token_type_ids
        self.cls_index = cls_index
        self.p_mask = p_mask

        self.example_index = example_index
        self.unique_id = unique_id
        self.paragraph_len = paragraph_len
        self.token_is_max_context = token_is_max_context
        self.tokens = tokens
        self.token_to_orig_map = token_to_orig_map

        self.start_position = start_position
        self.end_position = end_position
        self.is_impossible = is_impossible
        self.qas_id = qas_id
        self.query_end = query_end

        self.encoding = encoding

class QuacResult:
    """
    Constructs a QuacResult which can be used to evaluate a model's output on the QuAC dataset.

    Args:
        unique_id: The unique identifier corresponding to that example.
        start_logits: The logits corresponding to the start of the answer
        end_logits: The logits corresponding to the end of the answer
    """

    def __init__(self, unique_id, start_logits, end_logits, cls_logits):
        self.start_logits = start_logits
        self.end_logits = end_logits
        self.cls_logits = cls_logits    
        self.unique_id = unique_id