from CC.loaders.utils import *
from torch.utils.data import TensorDataset, DataLoader, Dataset
from torch import tensor
from transformers import BertTokenizer
from tqdm import *
from typing import *
from ICCSupervised.ICCSupervised import IDataLoader
import json
import numpy as np
import random
from distutils.util import strtobool


class LXLoader(IDataLoader):
    def __init__(self, **args):
        KwargsParser(debug=True) \
            .add_argument("batch_size", int, defaultValue=4) \
            .add_argument("eval_batch_size", int, defaultValue=16) \
            .add_argument("test_batch_size", int, defaultValue=16) \
            .add_argument("word_embedding_file", str) \
            .add_argument("word_vocab_file", str) \
            .add_argument("word_vocab_file_with_tag", str) \
            .add_argument("train_file", str) \
            .add_argument("eval_file", str) \
            .add_argument("test_file", str) \
            .add_argument("tag_file", str) \
            .add_argument("bert_vocab_file", str) \
            .add_argument("output_eval", bool, defaultValue=True) \
            .add_argument("max_scan_num", int, defaultValue=1000000) \
            .add_argument("add_seq_vocab", bool, defaultValue=False) \
            .add_argument("max_seq_length", int, defaultValue=256) \
            .add_argument("max_word_num", int, defaultValue=5) \
            .add_argument("default_tag", str, defaultValue="O") \
            .add_argument("use_test", bool, defaultValue=False) \
            .add_argument("do_shuffle", bool, defaultValue=False) \
            .add_argument("do_predict", bool, defaultValue=False) \
            .add_argument("lexicon_tree_cache_path", str, optional=True) \
            .add_argument("word_vacab_cache_path", str, optional=True) \
            .add_argument("task_name", str) \
            .add_argument("tag_rules", dict) \
            .add_argument("debug", bool, defaultValue=False) \
            .parse(self, **args)

        self.read_data_set()
        self.verify_data()
        self.process_data()

    def __restore_attr(self, attr_name: str, construct, restore_path: str = None):
        if restore_path is None:
            # If the restore path is not set, directly construct
            setattr(self, attr_name, construct())
        else:
            # If restore file exists, restore it
            if os.path.exists(restore_path):
                with open(restore_path, "rb") as f:
                    setattr(self, attr_name, pickle.load(f))
            else:
                obj = construct()
                setattr(self, attr_name, obj)
                # save cache file for restore
                with open(restore_path, "wb") as f:
                    pickle.dump(obj, f)
        return self

    def read_data_set(self):
        self.data_files = [self.train_file, self.eval_file, self.test_file]
        # loading lexicon tree

        self.__restore_attr("lexicon_tree", lambda: TrieFactory.get_trie_from_vocabs(
            [self.word_vocab_file], self.max_scan_num), getattr(self, "lexicon_tree_cache_path", None))

        self.matched_words: List[str] = TrieFactory.get_all_matched_word_from_dataset(
            self.data_files, self.lexicon_tree)
        # restore all word_vocab_file_with_tag
        self.__restore_attr("word_vocab", lambda: VocabTag().from_files(
            [self.word_vocab_file_with_tag], is_word=True, has_default=False, unk_num=5, max_scan_num=self.max_scan_num), getattr(self, "word_vocab_cache_path", None))

        matched_words_with_tags = [(word, self.word_vocab.tag(word))
                                   for word in self.matched_words]
        # re assign word_vocab for matched_words
        self.word_vocab = VocabTag().from_list(matched_words_with_tags,
                                               is_word=True, has_default=False, unk_num=5)

        self.tag_vocab = Vocab().from_files([self.tag_file])

        self.vocab_embedding, self.embedding_dim = VocabEmbedding(self.word_vocab, cache_dir=f"./temp/{self.task_name}").build_from_file(
            self.word_embedding_file, self.max_scan_num, self.add_seq_vocab).get_embedding()

        self.tag_convert: TagConvert = TagConvert(self.tag_rules)
        self.tokenizer = BertTokenizer.from_pretrained(self.bert_vocab_file)

    def verify_data(self):
        pass

    def process_data(self):
        if self.use_test:
            self.myData_test = LEXBertDataSet(self.data_files[2], **vars(self))
            self.dataiter_test = DataLoader(
                self.myData_test, batch_size=self.test_batch_size)
        else:
            self.myData = LEXBertDataSet(self.data_files[0], **vars(self))
            self.dataiter = DataLoader(self.myData, batch_size=self.batch_size)
            if self.output_eval:
                self.myData_eval = LEXBertDataSet(
                    self.data_files[1], **vars(self))
                self.dataiter_eval = DataLoader(
                    self.myData_eval, batch_size=self.eval_batch_size)


class LEXBertDataSet(Dataset):
    def __init__(self, dataset_file: str, **args):
        self.file = dataset_file
        for name in args.keys():
            setattr(self, name, args[name])
        self.__init_dataset()

    def convert_embedding(self, item):
        if "text" not in item:
            raise KeyError(f"key text not exists in item: {item}")
        if not self.do_predict:
            if "label" not in item:
                raise KeyError(f"key label not exists in item: {item}")
            # resolve prompt
            prompts = []
            prompt_masks = []
            prompt_tags = []
            word = []
            labels = []
            exist_prompt = set()
            for ch, label in zip(item["text"], item["label"]):
                if label != self.default_tag:
                    if label.startswith('B-'):
                        if len(word) != 0:
                            prompt, prompt_mask, prompt_tag = self.tag_convert.tag2prompt(
                                labels, word)
                            key = hash(str(prompt))
                            if key not in exist_prompt:
                                exist_prompt.add(key)
                                prompts.append(prompt)
                                prompt_masks.append(prompt_mask)
                                prompt_tags.append(prompt_tag)
                            word = []
                            labels = []
                    word.append(ch)
                    labels.append(label)
            if len(word) != 0:
                prompt, prompt_mask, prompt_tag = self.tag_convert.tag2prompt(
                    labels, word)
                key = hash(str(prompt))
                if key not in exist_prompt:
                    exist_prompt.add(key)
                    prompts.append(prompt)
                    prompt_masks.append(prompt_mask)
                    prompt_tags.append(prompt_tag)
            # resolve input
            text = ["[CLS]"] + item["text"][:self.max_seq_length-2]+["[SEP]"]
            text_origin_length = len(text)

            matched_words = self.lexicon_tree.getAllMatchedWordList(
                text, self.max_word_num)
            # for matched_word add prompt
            for words in matched_words:
                for word in words:
                    tag = self.word_vocab.tag(word)
                    # if the word tag is "O", skip...
                    if tag[0] == self.default_tag:
                        continue
                    prompt, prompt_mask, prompt_tag = self.tag_convert.tag2prompt(
                        tag, word)
                    key = hash(str(prompt))
                    if key not in exist_prompt:
                        exist_prompt.add(key)
                        prompts.append(prompt)
                        prompt_masks.append(prompt_mask)
                        prompt_tags.append(prompt_tag)

            label = [self.default_tag] + \
                item["label"][:self.max_seq_length-2]+[self.default_tag]
            mask = [1 for _ in text]
            for prompt, prompt_mask, prompt_tag in zip(prompts, prompt_masks, prompt_tags):
                if len(text)+len(prompt) <= self.max_seq_length:
                    text += prompt
                    label += prompt_tag
                    mask += prompt_mask

            # convert to ids
            token_ids = self.tokenizer.convert_tokens_to_ids(text)
            label_ids = self.tag_vocab.token2id(label)

            labels = []
            for m, token_id in zip(mask, token_ids):
                if m == 0:
                    labels.append(token_id)
                else:
                    labels.append(-100)

            np_input_ids = np.zeros(self.max_seq_length, dtype=np.int)
            np_input_ids[:len(token_ids)] = token_ids

            np_token_type_ids = np.ones(self.max_seq_length, dtype=np.int)
            np_token_type_ids[:text_origin_length] = 0

            np_attention_mask = np.ones(self.max_seq_length, dtype=np.int)
            np_attention_mask[:len(mask)] = mask

            np_label_ids = np.zeros(self.max_seq_length, dtype=np.int)
            np_label_ids[:len(label_ids)] = label_ids

            np_labels = np.zeros(self.max_seq_length, dtype=np.int)
            np_labels[:len(labels)] = labels
            np_labels[len(labels):] = -100

            np_matched_word_ids = np.zeros(
                (self.max_seq_length, self.max_word_num), dtype=np.int)
            np_matched_word_mask = np.zeros(
                (self.max_seq_length, self.max_word_num), dtype=np.int)
            np_matched_word_label_ids = np.zeros(
                (self.max_seq_length, self.max_word_num), dtype=np.int)

            for i, words in enumerate(matched_words):
                words = words[:self.max_word_num]

                # convert tag to chinese
                def convert_to_chinese(tag):
                    if tag == self.default_tag:
                        return '<pad>'
                    else:
                        return self.tag_rules[tag.split('-')[-1]]
                tags = [convert_to_chinese(tag[0])
                        for tag in self.word_vocab.tag(words)]

                tags = self.word_vocab.token2id(tags)
                word_ids = self.word_vocab.token2id(words)

                np_matched_word_ids[i][:len(
                    word_ids)] = word_ids
                np_matched_word_mask[i][:len(word_ids)] = 1
                np_matched_word_label_ids[i][:len(
                    tags)] = tags

            assert np_input_ids.shape[0] == np_token_type_ids.shape[0]
            assert np_input_ids.shape[0] == np_attention_mask.shape[0]
            assert np_input_ids.shape[0] == np_matched_word_ids.shape[0]
            assert np_input_ids.shape[0] == np_matched_word_mask.shape[0]
            assert np_input_ids.shape[0] == np_label_ids.shape[0]
            assert np_input_ids.shape[0] == np_labels.shape[0]
            assert np_matched_word_ids.shape[1] == np_matched_word_mask.shape[1]
            assert np_matched_word_ids.shape[1] == np_matched_word_label_ids.shape[1]
            assert np_matched_word_ids.shape[1] == self.max_word_num

            return np_input_ids, np_token_type_ids, np_attention_mask, np_matched_word_ids, np_matched_word_mask, np_matched_word_label_ids, np_labels, np_label_ids
        # TODO: predict

        raise NotImplemented("do_predict not implement")

    def __init_dataset(self):
        line_total = FileUtil.count_lines(self.file)
        self.input_token_ids = []
        self.token_type_ids = []
        self.attention_mask = []
        self.matched_word_ids = []
        self.matched_word_mask = []
        self.matched_word_label_ids = []
        self.input_labels = []
        self.labels = []

        for line in tqdm(FileUtil.line_iter(self.file), desc=f"load dataset from {self.file}", total=line_total):
            line = line.strip()
            data: Dict[str, List[Any]] = json.loads(line)
            input_token_ids, token_type_ids, attention_mask, matched_word_ids, matched_word_mask, matched_word_label_ids, input_labels,  labels = self.convert_embedding(
                data)
            self.input_token_ids.append(input_token_ids)
            self.token_type_ids.append(token_type_ids)
            self.attention_mask.append(attention_mask)
            self.matched_word_ids.append(matched_word_ids)
            self.matched_word_mask.append(matched_word_mask)
            self.matched_word_label_ids.append(matched_word_label_ids)
            self.input_labels.append(input_labels)
            self.labels.append(labels)
        self.size = len(self.input_token_ids)
        self.input_token_ids = np.array(self.input_token_ids)
        self.token_type_ids = np.array(self.token_type_ids)
        self.attention_mask = np.array(self.attention_mask)
        self.matched_word_ids = np.array(self.matched_word_ids)
        self.matched_word_mask = np.array(self.matched_word_mask)
        self.matched_word_label_ids = np.array(self.matched_word_label_ids)
        self.input_labels = np.array(self.input_labels)
        self.labels = np.array(self.labels)
        self.indexes = [i for i in range(self.size)]
        if self.do_shuffle:
            random.shuffle(self.indexes)

    def __getitem__(self, idx):
        idx = self.indexes[idx]
        return {
            'input_ids': tensor(self.input_token_ids[idx]),
            'attention_mask': tensor(self.attention_mask[idx]),
            'token_type_ids': tensor(self.token_type_ids[idx]),
            'matched_word_ids': tensor(self.matched_word_ids[idx]),
            'matched_word_mask': tensor(self.matched_word_mask[idx]),
            'matched_word_label_ids': tensor(self.matched_word_label_ids[idx]),
            'input_labels': tensor(self.input_labels[idx]),
            'labels': tensor(self.labels[idx])
        }

    def __len__(self):
        return self.size
