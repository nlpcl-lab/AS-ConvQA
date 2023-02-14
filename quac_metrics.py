import collections
import logging
import json
import math
import re, string

from collections import defaultdict, Counter
from transformers import BasicTokenizer

logging.basicConfig(format = '%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt = '%m/%d/%Y %H:%M:%S',
                    level = logging.INFO)
logger = logging.getLogger(__name__)


def normalize_answer(s):
    """Lower text and remove punctuation, articles and extra whitespace."""
    def remove_articles(text):
        return re.sub(r'\b(a|an|the)\b', ' ', text)
    def white_space_fix(text):
        return ' '.join(text.split())
    def remove_punc(text):
        exclude = set(string.punctuation)
        return ''.join(ch for ch in text if ch not in exclude)
    def lower(text):
        return text.lower()

    return white_space_fix(remove_articles(remove_punc(lower(s))))


def f1_score(prediction, ground_truth):
    prediction_tokens = normalize_answer(prediction).split()
    ground_truth_tokens = normalize_answer(ground_truth).split()
    common = Counter(prediction_tokens) & Counter(ground_truth_tokens)
    num_same = sum(common.values())
    if num_same == 0:
        return 0
    precision = 1.0 * num_same / len(prediction_tokens)
    recall = 1.0 * num_same / len(ground_truth_tokens)
    f1 = (2 * precision * recall) / (precision + recall)
    return f1


def single_score(prediction, ground_truth):
    if prediction == "CANNOTANSWER" and ground_truth == "CANNOTANSWER":
        return 1.0
    elif prediction == "CANNOTANSWER" or ground_truth == "CANNOTANSWER":
        return 0.0
    else:
        return f1_score(prediction, ground_truth)


def get_final_text(pred_text, orig_text, do_lower_case, verbose_logging=False):
    """Project the tokenized prediction back to the original text."""

    # When we created the data, we kept track of the alignment between original
    # (whitespace tokenized) tokens and our WordPiece tokenized tokens. So
    # now `orig_text` contains the span of our original text corresponding to the
    # span that we predicted.
    #
    # However, `orig_text` may contain extra characters that we don't want in
    # our prediction.
    #
    # For example, let's say:
    #   pred_text = steve smith
    #   orig_text = Steve Smith's
    #
    # We don't want to return `orig_text` because it contains the extra "'s".
    #
    # We don't want to return `pred_text` because it's already been normalized
    #
    # What we really want to return is "Steve Smith".
    #
    # Therefore, we have to apply a semi-complicated alignment heuristic between
    # `pred_text` and `orig_text` to get a character-to-character alignment. This
    # can fail in certain cases in which case we just return `orig_text`.

    def _strip_spaces(text):
        ns_chars = []
        ns_to_s_map = collections.OrderedDict()
        for (i, c) in enumerate(text):
            if c == " ":
                continue
            ns_to_s_map[len(ns_chars)] = i
            ns_chars.append(c)
        ns_text = "".join(ns_chars)
        return (ns_text, ns_to_s_map)

    # We first tokenize `orig_text`, strip whitespace from the result
    # and `pred_text`, and check if they are the same length. If they are
    # NOT the same length, the heuristic has failed. If they are the same
    # length, we assume the characters are one-to-one aligned.
    tokenizer = BasicTokenizer(do_lower_case=do_lower_case)

    tok_text = " ".join(tokenizer.tokenize(orig_text))

    start_position = tok_text.find(pred_text)
    if start_position == -1:
        if verbose_logging:
            logger.info("Unable to find text: '%s' in '%s'" % (pred_text, orig_text))
        return orig_text
    end_position = start_position + len(pred_text) - 1

    (orig_ns_text, orig_ns_to_s_map) = _strip_spaces(orig_text)
    (tok_ns_text, tok_ns_to_s_map) = _strip_spaces(tok_text)

    if len(orig_ns_text) != len(tok_ns_text):
        if verbose_logging:
            logger.info("Length not equal after stripping spaces: '%s' vs '%s'", orig_ns_text, tok_ns_text)
        return orig_text

    # We then project the characters in `pred_text` back to `orig_text` using
    # the character-to-character alignment.
    tok_s_to_ns_map = {}
    for (i, tok_index) in tok_ns_to_s_map.items():
        tok_s_to_ns_map[tok_index] = i

    orig_start_position = None
    if start_position in tok_s_to_ns_map:
        ns_start_position = tok_s_to_ns_map[start_position]
        if ns_start_position in orig_ns_to_s_map:
            orig_start_position = orig_ns_to_s_map[ns_start_position]

    if orig_start_position is None:
        if verbose_logging:
            logger.info("Couldn't map start position")
        return orig_text

    orig_end_position = None
    if end_position in tok_s_to_ns_map:
        ns_end_position = tok_s_to_ns_map[end_position]
        if ns_end_position in orig_ns_to_s_map:
            orig_end_position = orig_ns_to_s_map[ns_end_position]

    if orig_end_position is None:
        if verbose_logging:
            logger.info("Couldn't map end position")
        return orig_text

    output_text = orig_text[orig_start_position : (orig_end_position + 1)]
    return output_text


def read_target_dict(input_file):
    target = json.load(open(input_file))['data']
    target_dict = {}
    for p in target:
        for par in p['paragraphs']:
            p_id = par['id']
            qa_list = par['qas']
            for qa in qa_list:
                q_idx = qa['id']
                val_spans = [anss['text'] for anss in qa['answers']]
                target_dict[q_idx] = val_spans
    
    return target_dict


def read_target_dict_exclude_goldCannotAnswer(input_file):
    target = json.load(open(input_file))['data']
    target_dict = {}
    for p in target:
        for par in p['paragraphs']:
            p_id = par['id']
            qa_list = par['qas']
            for qa in qa_list:
                q_idx = qa['id']
                val_spans = [anss['text'] for anss in qa['answers'] if anss['text'] != 'CANNOTANSWER']
                if val_spans == []:
                    continue
                # val_spans_ = [anss['text'] for anss in qa['answers']]
                # if val_spans != val_spans_:
                #     import pdb; pdb.set_trace()

                target_dict[q_idx] = val_spans
    
    return target_dict


def leave_one_out(refs):
    if len(refs) == 1:
        return 1.
    splits = []
    for r in refs:
        splits.append(r.split())
    t_f1 = 0.0
    for i in range(len(refs)):
        m_f1 = 0
        for j in range(len(refs)):
            if i == j:
                continue
            f1_ij = f1_score(refs[i], refs[j])
            if f1_ij > m_f1:
                m_f1 = f1_ij
        t_f1 += m_f1
    return t_f1 / len(refs)


def leave_one_out_max(prediction, ground_truths):
    scores_for_ground_truths = []
    for ground_truth in ground_truths:
        scores_for_ground_truths.append(single_score(prediction, ground_truth))

    if len(scores_for_ground_truths) == 1:
        return scores_for_ground_truths[0]
    else:
        # leave out one ref every time
        t_f1 = []
        for i in range(len(scores_for_ground_truths)):
            t_f1.append(max(scores_for_ground_truths[:i] + scores_for_ground_truths[i+1:]))
        return 1.0 * sum(t_f1) / len(t_f1)


def handle_cannot(refs):
    num_cannot = 0
    num_spans = 0
    for ref in refs:
        if ref == 'CANNOTANSWER':
            num_cannot += 1
        else:
            num_spans += 1
    if num_cannot >= num_spans:
        refs = ['CANNOTANSWER']
    else:
        refs = [x for x in refs if x != 'CANNOTANSWER']
    return refs


def _get_best_indexes(logits, n_best_size):
    """Get the n-best logits from a list."""
    index_and_score = sorted(enumerate(logits), key=lambda x: x[1], reverse=True)

    best_indexes = []
    for i in range(len(index_and_score)):
        if i >= n_best_size:
            break
        best_indexes.append(index_and_score[i][0])
    return best_indexes


def _compute_softmax(scores):
    """Compute softmax probability over raw logits."""
    if not scores:
        return []

    max_score = None
    for score in scores:
        if max_score is None or score > max_score:
            max_score = score

    exp_scores = []
    total_sum = 0.0
    for score in scores:
        x = math.exp(score - max_score)
        exp_scores.append(x)
        total_sum += x

    probs = []
    for score in exp_scores:
        probs.append(score / total_sum)
    return probs

def compute_predictions_logits(
    all_examples,
    all_features,
    all_results,
    n_best_size,
    max_answer_length,
    do_lower_case,
    output_prediction_file,
    output_nbest_file,
    output_null_log_odds_file,
    output_nbest_with_start_index_file, #TODO
    verbose_logging,
    null_score_diff_threshold,
    tokenizer,
    write_predictions=True,
    exclude_cannotanswer=False
):
    """Write final predictions to the json file and log-odds of null if needed."""
    if output_prediction_file:
        logger.info(f"Writing predictions to: {output_prediction_file}")
    if output_nbest_file:
        logger.info(f"Writing nbest to: {output_nbest_file}")
    if output_null_log_odds_file:
        logger.info(f"Writing null_log_odds to: {output_null_log_odds_file}")
    #TODO
    if output_nbest_with_start_index_file:
        logger.info(f"Writing nbest with start index to: {output_nbest_with_start_index_file}")

    example_index_to_features = collections.defaultdict(list)
    for feature in all_features:
        example_index_to_features[feature.example_index].append(feature)

    unique_id_to_result = {}
    for result in all_results:
        unique_id_to_result[result.unique_id] = result

    _PrelimPrediction = collections.namedtuple(  # pylint: disable=invalid-name
    "PrelimPrediction",
        ["feature_index", "start_index", "end_index", "start_logit", "end_logit", "class_logit"]
    )

    all_predictions = collections.OrderedDict()
    all_nbest_json = collections.OrderedDict()
    scores_diff_json = collections.OrderedDict()
    #TODO
    all_nbest_start_json = collections.OrderedDict()

    for (example_index, example) in enumerate(all_examples):

        if exclude_cannotanswer:
            if example_index_to_features[example_index] == []: # we have excluded cannotanswer examples, so example_index may not be same as feature.example_index.
                #import pdb; pdb.set_trace()
                continue
            
        features = example_index_to_features[example_index]

        prelim_predictions = []
        # keep track of the minimum score of null start+end of position 0
        score_null = 1000000  # large and positive
        min_null_feature_index = 0  # the paragraph slice with min null score
        null_start_logit = 0  # the start logit at the slice with min null score
        null_end_logit = 0  # the end logit at the slice with min null score
        null_class_logit = None
        
        for (feature_index, feature) in enumerate(features):
            result = unique_id_to_result[feature.unique_id]
            start_indexes = _get_best_indexes(result.start_logits, n_best_size)
            end_indexes = _get_best_indexes(result.end_logits, n_best_size)
            # if we could have irrelevant answers, get the min score of irrelevant
            feature_null_score = result.start_logits[0] + result.end_logits[0]
            if feature_null_score < score_null:
                score_null = feature_null_score
                min_null_feature_index = feature_index
                null_start_logit = result.start_logits[0]
                null_end_logit = result.end_logits[0]
                null_class_logit = result.cls_logits

            for start_index in start_indexes:
                for end_index in end_indexes:
                    # We could hypothetically create invalid predictions, e.g., predict
                    # that the start of the span is in the question. We throw out all
                    # invalid predictions.
                    if start_index >= len(feature.tokens):
                        continue
                    if end_index >= len(feature.tokens):
                        continue
                    if start_index not in feature.token_to_orig_map:
                        continue
                    if end_index not in feature.token_to_orig_map:
                        continue
                    if not feature.token_is_max_context.get(start_index, False):
                        continue
                    if end_index < start_index:
                        continue
                    length = end_index - start_index + 1
                    if length > max_answer_length:
                        continue
                    prelim_predictions.append(
                        _PrelimPrediction(
                            feature_index=feature_index,
                            start_index=start_index,
                            end_index=end_index,
                            start_logit=result.start_logits[start_index],
                            end_logit=result.end_logits[end_index],
                            class_logit=result.cls_logits
                        )
                    )
        prelim_predictions.append(
            _PrelimPrediction(
                feature_index=min_null_feature_index,
                start_index=0,
                end_index=0,
                start_logit=null_start_logit,
                end_logit=null_end_logit,
                class_logit=null_class_logit
            )
        )
        prelim_predictions = sorted(prelim_predictions, key=lambda x: (x.start_logit + x.end_logit), reverse=True)

        _NbestPrediction = collections.namedtuple(  # pylint: disable=invalid-name
            "NbestPrediction", ["text", "start_logit", "end_logit"]
        )
        #TODO
        _NbestPredictionStart = collections.namedtuple(  # pylint: disable=invalid-name
            "NbestPredictionStart", ["text", "start_logit", "end_logit", "answer_start"]
        )

        seen_predictions = {}
        nbest = []
        #TODO
        nbest_start = []
        for pred in prelim_predictions:
            if len(nbest) >= n_best_size:
                break
            #import pdb; pdb.set_trace()
            # try :
            #     feature = features[pred.feature_index]
            # except:
            #     import pdb; pdb.set_trace()
            feature = features[pred.feature_index]
            if pred.start_index > 0:  # this is a non-null prediction
                tok_tokens = feature.tokens[pred.start_index : (pred.end_index + 1)]
                orig_doc_start = feature.token_to_orig_map[pred.start_index]
                orig_doc_end = feature.token_to_orig_map[pred.end_index]
                orig_tokens = example.doc_tokens[orig_doc_start : (orig_doc_end + 1)]
                tok_text = tokenizer.convert_tokens_to_string(tok_tokens)
                
                # Clean whitespace
                tok_text = tok_text.strip()
                tok_text = " ".join(tok_text.split())
                orig_text = " ".join(orig_tokens)
                
                final_text = get_final_text(tok_text, orig_text, do_lower_case, verbose_logging)
                
                #TODO
                actual_doc_text = example.context_text#" ".join(example.doc_tokens)
                answer_start = actual_doc_text.find(final_text)
                #import pdb; pdb.set_trace()

                if final_text in seen_predictions:
                    continue

                seen_predictions[final_text] = True
            else:
                final_text = 'CANNOTANSWER'
                seen_predictions[final_text] = True
                #TODO
                actual_doc_text = example.context_text#" ".join(example.doc_tokens)
                answer_start = actual_doc_text.find(final_text)
                #import pdb; pdb.set_trace()
                
            nbest.append(_NbestPrediction(text=final_text, start_logit=pred.start_logit, end_logit=pred.end_logit))
            #TODO
            nbest_start.append(_NbestPredictionStart(text=final_text, start_logit=pred.start_logit, end_logit=pred.end_logit,
             answer_start=answer_start))

        # if we didn't include the empty option in the n-best, include it
        if "CANNOTANSWER" not in seen_predictions:
            nbest.append(_NbestPrediction(text="CANNOTANSWER", start_logit=null_start_logit, end_logit=null_end_logit))
            #TODO
            nbest_start.append(_NbestPredictionStart(text="CANNOTANSWER", start_logit=null_start_logit, end_logit=null_end_logit,
             answer_start=answer_start))

        # In very rare edge cases we could have no valid predictions. So we
        # just create a nonce prediction in this case to avoid failure.
        if not nbest:
            nbest.append(_NbestPrediction(text="CANNOTANSWER", start_logit=0.0, end_logit=0.0))
            #TODO
            nbest_start.append(_NbestPredictionStart(text="CANNOTANSWER", start_logit=0.0, end_logit=0.0,
             answer_start=answer_start))

        assert len(nbest) >= 1, "No valid predictions"

        total_scores = []
        best_non_null_entry = None
        for entry in nbest:
            total_scores.append(entry.start_logit + entry.end_logit)
            if not best_non_null_entry:
                if entry.text != "CANNOTANSWER":
                    best_non_null_entry = entry
                    
        probs = _compute_softmax(total_scores)

        nbest_json = []
        for (i, entry) in enumerate(nbest):
            output = collections.OrderedDict()
            output["text"] = entry.text
            output["probability"] = probs[i]
            output["start_logit"] = entry.start_logit
            output["end_logit"] = entry.end_logit
            #TODO
            #output["start_index"] = pred.start_index

            nbest_json.append(output)

        assert len(nbest_json) >= 1, "No valid predictions"

        #TODO
        nbest_start_json = []
        for (i, entry_start) in enumerate(nbest_start):
            output = collections.OrderedDict()
            output["text"] = entry_start.text
            output["probability"] = probs[i]
            output["start_logit"] = entry_start.start_logit
            output["end_logit"] = entry_start.end_logit
            output["answer_start"] = entry_start.answer_start

            nbest_start_json.append(output)

        if not best_non_null_entry:
            score_diff = 10
        else:
            # predict "CANNOTANSWER" iff the null score - the score of best non-null > threshold
            score_diff = score_null - best_non_null_entry.start_logit - (best_non_null_entry.end_logit)
        scores_diff_json[example.qas_id] = score_diff
        if score_diff > null_score_diff_threshold:
            all_predictions[example.qas_id] = "CANNOTANSWER"
        else:
            all_predictions[example.qas_id] = best_non_null_entry.text
        all_nbest_json[example.qas_id] = nbest_json
        #TODO
        all_nbest_start_json[example.qas_id] = nbest_start_json

    if write_predictions:
        if output_prediction_file:
            with open(output_prediction_file, "w") as writer:
                writer.write(json.dumps(all_predictions, indent=4) + "\n")

        if output_nbest_file:
            with open(output_nbest_file, "w") as writer:
                writer.write(json.dumps(all_nbest_json, indent=4) + "\n")

        if output_null_log_odds_file:
            with open(output_null_log_odds_file, "w") as writer:
                writer.write(json.dumps(scores_diff_json, indent=4) + "\n")
        #TODO
        if output_nbest_with_start_index_file:
            with open(output_nbest_with_start_index_file, "w") as writer:
                writer.write(json.dumps(all_nbest_start_json, indent=4) + "\n")

    return all_predictions, all_nbest_json


def write_quac(prediction, nbest_pred, in_file, out_file):
    dialog_pred = defaultdict(list)
    for qa_id, span in prediction.items():
        dialog_id = qa_id.split("_q#")[0]

        if len(span) == 0:
            span = 'CANNOTANSWER'
        dialog_pred[dialog_id].append([qa_id, span])

    # for those we don't predict anything
    target = json.load(open(in_file))['data']
    for p in target:
        for par in p['paragraphs']:
            dialog_id = par['id']
            qa_list = par['qas']
            if dialog_id not in dialog_pred:
                for qa in qa_list:
                    qa_id = qa['id']
                    dialog_pred[dialog_id].append([qa_id, 'CANNOTANSWER'])

    # now we predict
    with open(out_file, 'w') as fout:
        for dialog_id, dialog_span in dialog_pred.items():
            output_dict = {'best_span_str': [], 'qid': [], 'yesno':[], 'followup': []}
            for qa_id, span in dialog_span:
                output_dict['best_span_str'].append(span)
                output_dict['qid'].append(qa_id)
                output_dict['yesno'].append('y')
                output_dict['followup'].append('y')
            fout.write(json.dumps(output_dict) + '\n')


def quac_performance(prediction, target_dict):
    pred, truth = [], []

    for qa_id, span in prediction.items():
        dialog_id = qa_id.split("_q#")[0]
        
        if len(span) == 0:
            span = 'CANNOTANSWER'
        
        pred.append(span)
        truth.append(target_dict[qa_id])
    min_F1 = 0.4
    clean_pred, clean_truth = [], []

    all_f1 = []
    for p, t in zip(pred, truth):
        clean_t = handle_cannot(t)
        # compute human performance
        human_F1 = leave_one_out(clean_t)
        if human_F1 < min_F1: continue

        clean_pred.append(p)
        clean_truth.append(clean_t)
        all_f1.append(leave_one_out_max(p, clean_t))

    cur_f1, best_f1 = sum(all_f1), sum(all_f1)

    return 100.0 * best_f1 / len(clean_pred)

def quac_performance_exclude_goldCannotAnswer(prediction, target_dict):
    num_goldCannotAnswer = 0
    num_total_qas = 0
    pred, truth = [], []

    for qa_id, span in prediction.items():
        num_total_qas = num_total_qas + 1
        dialog_id = qa_id.split("_q#")[0]
        
        if len(span) == 0:
            span = 'CANNOTANSWER'
        
        if qa_id not in target_dict.keys():
            num_goldCannotAnswer = num_goldCannotAnswer + 1
            #import pdb; pdb.set_trace()
            continue

        pred.append(span)
        truth.append(target_dict[qa_id])

    min_F1 = 0.4
    clean_pred, clean_truth = [], []

    all_f1 = []
    for p, t in zip(pred, truth):
        clean_t = handle_cannot(t)
        # compute human performance
        human_F1 = leave_one_out(clean_t)
        if human_F1 < min_F1: continue

        clean_pred.append(p)
        clean_truth.append(clean_t)
        all_f1.append(leave_one_out_max(p, clean_t))

    cur_f1, best_f1 = sum(all_f1), sum(all_f1)

    return 100.0 * best_f1 / len(clean_pred), num_goldCannotAnswer, num_total_qas