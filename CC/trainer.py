import os
import re
import uuid
import torch
import torch.nn as nn
import torch.optim as optim
from torch.autograd import Variable
from transformers import BertConfig, BertTokenizer, BertModel, get_linear_schedule_with_warmup
from CC.crf import CRF
from tqdm import tqdm
from ICCSupervised.ICCSupervised import ITrainer
from CC.dataloader import AutoDataLoader
from CC.analysis import CCAnalysis
from CC.model import CCNERModel


class NERTrainer(ITrainer):

    def __init__(self, **args):
        '''
        Args:
        - num_epochs
        - num_gpus
        - bert_config_file_name
        - pretrained_file_name
        - hidden_dim
        - word_embedding_file: required in `l_loader`
        - word_vocab_file: required in `l_loader`
        - train_file
        - eval_file
        - test_file
        - tag_file
        - bert_vocab_file
        - batch_size
        - eval_batch_size
        - word_tag_split: optional in `cn_loader`, default: ' '
        - pattern: optional in `cn_loader`, default: '， O'
        - output_eval: optional, default: None
        - max_scan: optional in `l_loader`, default: None
        - add_seq_vocab: optional in `l_loader`, default: False
        - max_seq_length: optional in `l_loader`, default: 256
        - max_word_num: optional in `l_loader`, default: 5
        - default_tag: optional in `l_loader`, default: "O"
        - model_name: optional, default: "LBert"
        - loader_name: optional, default: "l_loader"
        - task_name: optional, default: None
        '''
        assert "num_epochs" in args, "argument num_epochs: required embeding file path"
        assert "num_gpus" in args, "argument num_gpus: required embeding file path"
        assert "hidden_dim" in args, "argument hidden_dim: required embeding file path"
        self.model_name: str = 'LBert'
        if "model_name" in args:
            self.model_name = args["model_name"]
        self.loader_name = 'l_loader'
        if "loader_name" in args:
            self.loader_name = args["loader_name"]

        self.eval_data = args['output_eval']
        self.num_epochs = args['num_epochs']
        self.num_gpus = args['num_gpus']
        self.output_eval = args['output_eval']
        self.dataloader_init(**args)
        self.model_init(**args)
        self.task_name = args['task_name']

    def model_init(self, **args):
        model_args = {
            'model_name': self.model_name,
            'bert_config_file_name': args['bert_config_file_name'],
            'tagset_size': self.tag_size,
            'hidden_dim': args['hidden_dim']
        }
        if 'word_embedding_file' in args:
            model_args['pretrained_embeddings'] = self.vocab_embedding
        if 'pretrained_file_name' in args:
            model_args['pretrained_file_name'] = args['pretrained_file_name']

        self.bert_ner = CCNERModel(**model_args)
        self.model, self.birnncrf = self.bert_ner()

    def dataloader_init(self, **args):
        self.dataloader = AutoDataLoader(**args)
        result = self.dataloader()
        self.train_data = result['train_set']
        self.train_iter = result['train_iter']
        if self.loader_name == 'l_loader':
            self.vocab_embedding = result['vocab_embedding']
            self.embedding_dim = result['embedding_dim']
            self.tag_vocab = result['tag_vocab']
            self.tag_size = self.tag_vocab.__len__()
            self.analysis = CCAnalysis(
                self.tag_vocab.token2id, self.tag_vocab.id2token)
        if self.loader_name == 'cn_loader':
            self.dm = result['dm']
            self.tag_size = len(self.dm.tag_to_idx)
            self.analysis = CCAnalysis(self.dm.tagToIdx, self.dm.idxToTag)

        if self.output_eval is not None:
            self.eval_set = result['eval_set']
            self.eval_iter = result['eval_iter']

    def train(self, resume_path=False, resume_step=False):
        alpha = 1e-10

        optimizer = optim.AdamW([
            {'params': self.model.parameters(), 'lr': 2e-5},
            {'params': self.birnncrf.parameters(), 'lr': 1e-3}
        ], lr=1e-5, weight_decay=0.)
        scheduler = get_linear_schedule_with_warmup(optimizer, 190, 80000)
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        if torch.cuda.device_count() > 1:
            self.model = nn.DataParallel(self.model, device_ids=self.num_gpus)
            self.birnncrf.cuda()

        self.model.to(device)
        self.birnncrf.to(device)

        if not resume_path == False:
            print('Accessing Resume PATH: {} step: {} ...\n'.format(
                resume_path, resume_step))
            bert_model_dict = torch.load(os.path.join(
                resume_path, self.model_name, '{}_{}.pth'.format(self.model_name, resume_step))).module.state_dict()
            self.model.module.load_state_dict(bert_model_dict)
            self.birnncrf = torch.load(os.path.join(
                resume_path, 'lstm_crf', 'lstm_crf_{}.pth'.format(resume_step)))
            self.model.to(device)
            self.birnncrf.to(device)

        current_uid = str(uuid.uuid1()).split('-')[0]

        train_step = resume_step if resume_step != False else 0
        for epoch in range(self.num_epochs):
            train_count = 0
            train_pred_num = 0
            train_gold_num = 0
            train_correct_num = 0
            train_loss = 0
            train_iter = tqdm(self.train_iter)
            self.model.train()
            self.birnncrf.train()
            for it in train_iter:
                train_step += 1

                for key in it.keys():
                    it[key] = self.cuda(it[key])

                outputs = self.model(**it)
                hidden_states = outputs['mix_output']
                loss = self.birnncrf.loss(
                    hidden_states, it['input_ids'].gt(0), it['labels'])
                loss = loss.mean()

                loss.backward()
                optimizer.step()
                scheduler.step()  # Update learning rate schedule
                self.model.zero_grad()
                self.birnncrf.zero_grad()

                train_loss += loss.data.item()
                train_count += 1

                pred = self.birnncrf(hidden_states, it['input_ids'].gt(0))[1]

                t1, t2 = self.analysis.getPrecision(it['labels'], pred)
                train_pred_num += t1
                train_correct_num += t2
                train_gold_num += self.analysis.getRecall(it['labels'])

                train_acc = train_correct_num / train_pred_num if train_pred_num != 0 else 0
                train_recall = train_correct_num / train_gold_num if train_gold_num != 0 else 0

                train_iter.set_description(
                    'Epoch: {}/{} Train'.format(epoch + 1, self.num_epochs))
                train_iter.set_postfix(train_loss=train_loss / train_count, train_acc=train_acc,
                                       train_recall=train_recall, F1=(2 * train_acc * train_recall) / (train_acc + train_recall + alpha))
                self.analysis.append_train_record({
                    'loss': loss.data.item(),
                    'f1': (2 * train_acc * train_recall) / (train_acc + train_recall + alpha),
                    'acc': train_acc,
                    'recall': train_recall
                })

            model_uid = self.save_model(train_step)
            if self.eval_data:
                self.eval()

            self.analysis.save_csv(
                uid=current_uid if self.task_name is None else self.task_name)
            yield (epoch + 1, self.analysis.train_record, self.analysis.eval_record, self.analysis.model_record, model_uid)

    def eval(self):
        alpha = 1e-10

        test_count = 0
        test_pred_num = 0
        test_gold_num = 0
        test_correct_num = 0
        eval_loss = 0
        test_iter = tqdm(self.eval_iter)
        self.model.eval()
        self.birnncrf.eval()
        with torch.no_grad():
            for it in test_iter:
                for key in it.keys():
                    it[key] = self.cuda(it[key])

                outputs = self.model(**it)
                hidden_states = outputs['mix_output']
                loss = self.birnncrf.loss(
                    hidden_states, it['input_ids'].gt(0), it['labels'])
                loss = loss.mean()

                eval_loss += loss.data.item()
                test_count += 1

                pred = self.birnncrf(hidden_states, it['input_ids'].gt(0))[1]

                t1, t2 = self.analysis.getPrecision(it['labels'], pred)
                test_pred_num += t1
                test_correct_num += t2
                test_gold_num += self.analysis.getRecall(it['labels'])

                test_acc = test_correct_num / test_pred_num if test_pred_num != 0 else 0
                test_recall = test_correct_num / test_gold_num if test_gold_num != 0 else 0

                test_iter.set_description('Eval Result')
                test_iter.set_postfix(eval_loss=eval_loss / test_count, eval_acc=test_acc, eval_recall=test_recall,
                                      F1=(2 * test_acc * test_recall) / (test_acc + test_recall + alpha))
            self.analysis.append_eval_record({
                'loss': loss.data.item(),
                'f1': (2 * test_acc * test_recall) / (test_acc + test_recall + alpha),
                'acc': test_acc,
                'recall': test_recall
            })

    def save_model(self, current_step=0):
        if self.task_name is None:
            dir = 'undefined'
        else:
            dir = self.task_name
        if not os.path.exists('./save_model/{}/{}'.format(dir, self.model_name)):
            os.makedirs('./save_model/{}/{}'.format(dir, self.model_name))
        if not os.path.exists('./save_model/{}/lstm_crf'.format(dir)):
            os.makedirs('./save_model/{}/lstm_crf'.format(dir))
        torch.save(
            self.model, './save_model/{}/{}/{}_{}.pth'.format(dir, self.model_name, self.model_name, current_step))
        torch.save(self.birnncrf,
                   './save_model/{}/lstm_crf/lstm_crf_{}.pth'.format(dir, current_step))
        self.analysis.append_model_record(current_step)
        return current_step

    def cuda(self, inputX):
        if type(inputX) == tuple:
            if torch.cuda.is_available():
                result = []
                for item in inputX:
                    result.append(item.cuda())
                return result
            return inputX
        else:
            if torch.cuda.is_available():
                return inputX.cuda()
            return inputX

    def __call__(self, **arg):
        for r in self.train(**arg):
            yield r
