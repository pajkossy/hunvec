import functools
from itertools import izip

import numpy
import gzip

import theano
import theano.tensor as T

from pylearn2.models.model import Model
from pylearn2.space import CompositeSpace
from pylearn2.utils import sharedX
from pylearn2.termination_criteria import MonitorBased, And, EpochCounter
from pylearn2.train import Train
from pylearn2.train_extensions.best_params import MonitorBasedSaveBest
from pylearn2.training_algorithms import learning_rule
from pylearn2.training_algorithms.sgd import SGD, LinearDecayOverEpoch
from pylearn2.training_algorithms.sgd import MonitorBasedLRAdjuster
from pylearn2.train_extensions.plots import Plots, PlotManager

from hunvec.seqtag.word_tagger import WordTaggerNetwork
from hunvec.cost.seq_tagger_cost import SeqTaggerCost
from hunvec.utils.viterbi import viterbi
from hunvec.utils.fscore import FScCounter
from hunvec.datasets.tools import replace_numerals

class SequenceTaggerNetwork(Model):
    def __init__(self, dataset, w2i, t2i, featurizer,
                 edim=None, hdims=None, fedim=None,
                 max_epochs=100, use_momentum=False, lr=.01, lr_lin_decay=None,
                 lr_scale=False, lr_monitor_decay=False,
                 valid_stop=False, reg_factors=None, dropout=False,
                 dropout_params=None, embedding_init=None,
                 embedded_model=None, monitor_train=True, plot_monitor=None,
                 num=False):
        super(SequenceTaggerNetwork, self).__init__()
        self.vocab_size = dataset.vocab_size
        self.window_size = dataset.window_size
        self.total_feats = dataset.total_feats
        self.feat_num = dataset.feat_num
        self.n_classes = dataset.n_classes
        self.max_epochs = max_epochs
        if edim is None:
            edim = 50
        if hdims is None:
            hdims = [100]
        if fedim is None:
            fedim = 5
        self.edim = edim
        self.fedim = fedim
        self.hdims = hdims

        self.w2i = w2i
        self.t2i = t2i
        self.featurizer = featurizer

        self._create_tagger()

        A_value = numpy.random.uniform(low=-.1, high=.1,
                                       size=(self.n_classes + 2,
                                             self.n_classes))
        self.A = sharedX(A_value, name='A')
        self.use_momentum = use_momentum
        self.lr = lr
        self.lr_lin_decay = lr_lin_decay
        self.lr_monitor_decay = lr_monitor_decay
        self.lr_scale = lr_scale
        self.valid_stop = valid_stop
        self.reg_factors = reg_factors
        self.close_cache = {}
        self.dropout_params = dropout_params
        self.dropout = dropout or self.dropout_params is not None
        self.hdims = hdims
        self.monitor_train = monitor_train
        self.num = num
        self.plot_monitor = plot_monitor
        if embedding_init is not None:
            self.set_embedding_weights(embedding_init)

    def _create_tagger(self):
        self.tagger = WordTaggerNetwork(
            self.vocab_size, self.window_size, self.total_feats,
            self.feat_num, self.hdims, self.edim, self.fedim, self.n_classes)

    def _create_data_specs(self, dataset):
        self.input_space = CompositeSpace([
            dataset.data_specs[0].components[i]
            for i in xrange(len(dataset.data_specs[0].components) - 1)])
        self.output_space = dataset.data_specs[0].components[-1]

        self.input_source = dataset.data_specs[1][:-1]
        self.target_source = dataset.data_specs[1][-1]

    def __getstate__(self):
        d = {}
        d['vocab_size'] = self.vocab_size
        d['window_size'] = self.window_size
        d['feat_num'] = self.feat_num
        d['total_feats'] = self.total_feats
        d['n_classes'] = self.n_classes
        d['input_space'] = self.input_space
        d['output_space'] = self.output_space
        d['input_source'] = self.input_source
        d['target_source'] = self.target_source
        d['A'] = self.A
        d['tagger'] = self.tagger
        d['w2i'] = self.w2i
        d['t2i'] = self.t2i
        d['featurizer'] = self.featurizer
        d['max_epochs'] = self.max_epochs
        d['use_momentum'] = self.use_momentum
        d['lr'] = self.lr
        d['lr_lin_decay'] = self.lr_lin_decay
        d['lr_monitor_decay'] = self.lr_monitor_decay
        d['lr_scale'] = self.lr_scale
        d['valid_stop'] = self.valid_stop
        d['reg_factors'] = self.reg_factors
        d['dropout'] = self.dropout
        d['dropout_params'] = self.dropout_params
        d['monitor_train'] = self.monitor_train
        d['num'] = self.num
        d['plot_monitor'] = self.plot_monitor
        return d

    def fprop(self, data):
        tagger_out = self.tagger.fprop(data)
        probs = T.concatenate([self.A, tagger_out])
        return probs

    def dropout_fprop(self, data, default_input_include_prob=0.5,
                      input_include_probs=None, default_input_scale=2.0,
                      input_scales=None, per_example=True):
        if input_scales is None:
            input_scales = {'input': 1.0}
        if input_include_probs is None:
            input_include_probs = {'input': 1.0}
        if self.dropout_params is not None:
            if len(self.dropout_params) == len(self.tagger.layers) - 1:
                input_include_probs['tagger_out'] = self.dropout_params[-1]
                input_scales['tagger_out'] = 1.0/self.dropout_params[-1]
                for i, p in enumerate(self.dropout_params[:-1]):
                    input_include_probs['h{0}'.format(i)] = p
                    input_scales['h{0}'.format(i)] = 1.0/p
        tagger_out = self.tagger.dropout_fprop(
            data, default_input_include_prob, input_include_probs,
            default_input_scale, input_scales, per_example)
        probs = T.concatenate([self.A, tagger_out])
        return probs

    @functools.wraps(Model.get_lr_scalers)
    def get_lr_scalers(self):
        if not self.lr_scale:
            return {}
        d = self.tagger.get_lr_scalers()
        d[self.A] = 1. / self.n_classes
        return d

    @functools.wraps(Model.get_params)
    def get_params(self):
        return self.tagger.get_params() + [self.A]

    def create_adjustors(self):
        initial_momentum = .5
        final_momentum = .99
        start = 1
        saturate = self.max_epochs
        self.momentum_adjustor = learning_rule.MomentumAdjustor(
            final_momentum, start, saturate)
        self.momentum_rule = learning_rule.Momentum(initial_momentum,
                                                    nesterov_momentum=True)

        if self.lr_monitor_decay:
            self.learning_rate_adjustor = MonitorBasedLRAdjuster(
                high_trigger=1., shrink_amt=0.9,
                low_trigger=.95, grow_amt=1.1, channel_name='train_objective')
        elif self.lr_lin_decay:
            self.learning_rate_adjustor = LinearDecayOverEpoch(
                start, saturate, self.lr_lin_decay)

    def compute_used_inputs(self):
        seen = {'words': set(), 'feats': set()}
        for sen_w in self.dataset['train'].X1:
            seen['words'] |= reduce(
                lambda x, y: set(x) | set(y),
                sen_w, set())
        for sen_f in self.dataset['train'].X2:
            seen['feats'] |= reduce(
                lambda x, y: set(x) | set(y),
                sen_f, set())
        words = set(xrange(len(self.w2i)))
        feats = set(xrange(self.total_feats))
        self.notseen = {
            'words': numpy.array(sorted(words - seen['words'])),
            'feats': numpy.array(sorted(feats - seen['feats']))
        }

    def set_dataset(self, data):
        self._create_data_specs(data['train'])
        self.dataset = data
        self.compute_used_inputs()
        self.tagger.notseen = self.notseen

    def create_algorithm(self, data, save_best_path=None):
        self.set_dataset(data)
        self.create_adjustors()
        term = EpochCounter(max_epochs=self.max_epochs)
        if self.valid_stop:
            cost_crit = MonitorBased(channel_name='valid_objective',
                                     prop_decrease=.0, N=3)
            term = And(criteria=[cost_crit, term])

        #(layers, A_weight_decay)
        coeffs = None
        if self.reg_factors:
            rf = self.reg_factors
            lhdims = len(self.tagger.hdims)
            l_inputlayer = len(self.tagger.layers[0].layers)
            coeffs = ([[rf] * l_inputlayer] + ([rf] * lhdims) + [rf], rf)
        cost = SeqTaggerCost(coeffs, self.dropout)
        self.cost = cost

        self.mbsb = MonitorBasedSaveBest(channel_name='valid_objective',
                                         save_path=save_best_path)
        mon_dataset = dict(self.dataset)
        if not self.monitor_train:
            del mon_dataset['train']

        _learning_rule = (self.momentum_rule if self.use_momentum else None)
        self.algorithm = SGD(batch_size=1, learning_rate=self.lr,
                             termination_criterion=term,
                             monitoring_dataset=mon_dataset,
                             cost=cost,
                             learning_rule=_learning_rule,
                             )

        self.algorithm.setup(self, self.dataset['train'])
        if self.plot_monitor:
            cn = ["valid_objective", "test_objective"]
            if self.monitor_train:
                cn.append("train_objective")
            plots = Plots(channel_names=cn, save_path=self.plot_monitor)
            self.pm = PlotManager([plots], freq=1)
            self.pm.setup(self, None, self.algorithm)

    def train(self):
        while True:
            if not self.algorithm.continue_learning(self):
                break
            self.algorithm.train(dataset=self.dataset['train'])
            self.monitor.report_epoch()
            self.monitor()
            self.mbsb.on_monitor(self, self.dataset['valid'], self.algorithm)
            if self.use_momentum:
                self.momentum_adjustor.on_monitor(self, self.dataset['valid'],
                                                  self.algorithm)
            if hasattr(self, 'learning_rate_adjustor'):
                self.learning_rate_adjustor.on_monitor(
                    self, self.dataset['valid'], self.algorithm)
            if hasattr(self, 'pm'):
                self.pm.on_monitor(
                    self, self.dataset['valid'], self.algorithm)

    def prepare_tagging(self):
        X = self.get_input_space().make_theano_batch(batch_size=1)
        Y = self.fprop(X)
        self.f = theano.function([X[0], X[1]], Y)
        self.start = self.A.get_value()[0]
        self.end = self.A.get_value()[1]
        self.A_value = self.A.get_value()[2:]

    def process_input(self, words, feats):
        return self.f(words, feats)

    def tag_sen(self, words, feats, debug=False, return_probs=False):
        if not hasattr(self, 'f'):
            self.prepare_tagging()
        y = self.process_input(words, feats)
        tagger_out = y[2 + self.n_classes:]
        res = viterbi(self.start, self.A_value, self.end, tagger_out,
                               self.n_classes, return_probs)
        if return_probs:
            return res / res.sum(axis=1)[:,numpy.newaxis]
            #return res.reshape((1, len(res)))
        
        if debug:
            return numpy.array([[e] for e in res[1]]), tagger_out
        return numpy.array([[e] for e in res[1]])

    def get_score(self, dataset, mode='pwp'):
        self.prepare_tagging()
        tagged = (self.tag_sen(w, f) for w, f in
                  izip(dataset.X1, dataset.X2))
        gold = dataset.y
        good, bad = 0., 0.
        if mode == 'pwp':
            for t, g in izip(tagged, gold):
                g = g.argmax(axis=1)
                t = t.flatten()
                good += sum(t == g)
                bad += sum(t != g)
            return [good / (good + bad)]
        elif mode == 'f1':
            i2t = [t for t, i in sorted(self.t2i.items(), key=lambda x: x[1])]
            f1c = FScCounter(i2t, binary_input=False)
            gold = map(lambda x:x.argmax(axis=1), gold)
            tagged = map(lambda x:x.flatten(), tagged)
            return f1c.count_score(gold, tagged)

    def set_embedding_weights(self, embedding_init):
        # load embedding with gensim
        from gensim.models import Word2Vec
        try:
            m = Word2Vec.load_word2vec_format(embedding_init, binary=False)
            edim = m.layer1_size
        except UnicodeDecodeError:
            try:
                m = Word2Vec.load_word2vec_format(embedding_init, binary=True)
                edim = m.layer1_size
            except UnicodeDecodeError:
                # not in word2vec format
                m = Word2Vec.load(embedding_init)
                edim = m.layer1_size
        except ValueError:
            # glove model
            m = {}
            if embedding_init.endswith('gz'):
                fp = gzip.open(embedding_init)
            else:
                fp = open(embedding_init)
            for l in fp:
                le = l.split()
                m[le[0].decode('utf-8')] = numpy.array(
                    [float(e) for e in le[1:]], dtype=theano.config.floatX)
                edim = len(le) - 1

        if edim != self.edim:
            raise Exception("Embedding dim and edim doesn't match")
        m_lower = {}
        vocab = (m.vocab if hasattr(m, 'vocab') else m)
        for k in vocab:
            if k in ['UNKNOWN', 'PADDING']:
                continue
            if self.num:
                m_lower[replace_numerals(k.lower())] = m[k]
            else:
                m_lower[k.lower()] = m[k]
        # transform weight matrix with using self.w2i
        params = numpy.zeros(
            self.tagger.layers[0].layers[0].get_param_vector().shape, dtype=theano.config.floatX)
        e = self.edim
        for w in self.w2i:
            if w in m_lower:
                v = m_lower[w]
                i = self.w2i[w]
                params[i*e:(i+1)*e] = v
        if 'UNKNOWN' in vocab:
            params[-1*e:] = vocab['UNKNOWN']
        if 'PADDING' in vocab:
            params[-2*e:-1*e] = vocab['PADDING']
        self.tagger.layers[0].layers[0].set_param_vector(params)
