import sys
import functools

import numpy

import theano
import theano.tensor as T

from pylearn2.models.model import Model
from pylearn2.space import CompositeSpace
from pylearn2.utils import sharedX
from pylearn2.termination_criteria import MonitorBased, And, EpochCounter
from pylearn2.train import Train
from pylearn2.train_extensions.best_params import MonitorBasedSaveBest
from pylearn2.utils import serial
from pylearn2.training_algorithms import learning_rule
from pylearn2.training_algorithms.sgd import SGD, LinearDecay
from pylearn2.costs.cost import LpPenalty, SumOfCosts

from hunvec.seqtag.word_tagger import WordTaggerNetwork
from hunvec.seqtag.word_tagger_dataset import WordTaggerDataset
from hunvec.corpus.tagged_corpus import TaggedCorpus
from hunvec.feature.featurizer import Featurizer
from hunvec.cost.seq_tagger_cost import SeqTaggerCost


class SequenceTaggerNetwork(Model):
    def __init__(self, vocab_size, window_size, total_feats, feat_num,
                 hdim, edim, n_classes, dataset, max_epochs=100):

        super(SequenceTaggerNetwork, self).__init__()

        self.vocab_size = vocab_size
        self.window_size = window_size
        self.total_feats = total_feats
        self.feat_num = feat_num
        self.n_classes = n_classes
        self.max_epochs = max_epochs

        self.input_space = CompositeSpace([
            dataset.data_specs[0].components[0],
            dataset.data_specs[0].components[1],
        ])
        self.output_space = dataset.data_specs[0].components[2]

        self.input_source = ('words', 'features')
        self.target_source = 'targets'

        self.tagger = WordTaggerNetwork(vocab_size, window_size,
                                        self.total_feats, self.feat_num,
                                        hdim, edim, n_classes)

        A_value = numpy.random.uniform(low=-.1, high=.1,
                                       size=(self.n_classes + 2,
                                             self.n_classes))
        self.A = sharedX(A_value, name='A')

    def __getstate__(self):
        d = {}
        d['vocab_size'] = self.vocab_size
        d['window_size'] = self.window_size
        d['feat_num'] = self.feat_num
        d['n_classes'] = self.n_classes
        d['max_epochs'] = self.max_epochs
        d['input_space'] = self.input_space
        d['output_space'] = self.output_space
        d['input_source'] = self.input_source
        d['target_source'] = self.target_source
        d['A'] = self.A
        d['tagger'] = self.tagger
        return d

    def combine_A_tout_scanner(self, tagger_out, prev_res, A):
        # create a new matrix from A, add prev_res to every column
        A_t_ = A.dimshuffle((1, 0))
        A_t = A_t_ + prev_res

        log_added = A_t.max(axis=1)

        new_res = log_added + tagger_out
        return new_res

    def combined_scores(self, tagger_out):
        # compute normalizer factor NF for this given training data
        A = self.A
        start_M = tagger_out[0] + A[0]
        combined_probs, updates = theano.scan(
            fn=self.combine_A_tout_scanner,
            sequences=[tagger_out[1:]],
            non_sequences=[A[2:]],
            outputs_info=[start_M]
        )

        end_M = combined_probs[-1] + A[1]
        return T.concatenate([
            start_M.reshape((1, self.n_classes)),
            combined_probs[:-1],
            end_M.reshape((1, self.n_classes))])

    def fprop(self, data):
        tagger_out = self.tagger.fprop(data)
        return self.combined_scores(tagger_out)

    @functools.wraps(Model.get_params)
    def get_params(self):
        return self.tagger.get_params() + [self.A]

    @functools.wraps(Model.get_monitoring_channels)
    def get_monitoring_channels(self, data):
        rval = Model.get_monitoring_channels(self, data)
        rval['A_min'] = self.A.min()
        rval['A_max'] = self.A.max()
        rval['A_mean'] = self.A.mean()
        return rval

    def create_adjustors(self):
        initial_momentum = .5
        final_momentum = .99
        start = 1
        saturate = self.max_epochs
        self.momentum_adjustor = learning_rule.MomentumAdjustor(
            final_momentum, start, saturate)
        self.momentum_rule = learning_rule.Momentum(initial_momentum,
                                                    nesterov_momentum=True)

        decay_factor = .1
        self.learning_rate_adjustor = LinearDecay(
            start, saturate * 100, decay_factor)

    def create_algorithm(self, data, save_best_path=None):
        self.dataset = data
        self.create_adjustors()
        epoch_cnt_crit = EpochCounter(max_epochs=self.max_epochs)
        cost_crit = MonitorBased(channel_name='Prec',
                                 prop_decrease=0., N=10)
        term = And(criteria=[cost_crit, epoch_cnt_crit])

        #weightdecay = WeightDecay(coeffs=[5e-5, 5e-5, 5e-5])
        l2_A = LpPenalty([self.A], 2)
        cost = SumOfCosts(costs=[SeqTaggerCost(), l2_A])
        self.mbsb = MonitorBasedSaveBest(channel_name='objective',
                                         save_path=save_best_path)
        algorithm = SGD(batch_size=1, learning_rate=1e-2,
                        termination_criterion=term,
                        monitoring_dataset=data['valid'],
                        cost=cost,
                        learning_rule=self.momentum_rule,
                        update_callbacks=[self.learning_rate_adjustor],
                        )
        self.trainer = Train(dataset=data['train'], model=self,
                             algorithm=algorithm, extensions=[self.mbsb])


def test_data():
    params = {
        "vocab_size": 10,
        "window_size": 3,
        "feat_num": 2,
        "hdim": 10,
        "edim": 10,
        "n_classes": 2
    }

    # two sentences, with 2 and 3 lengths
    X = ([
         [[0, 1, 2], [3, 5, 4]],
         [[0, 1, 2], [3, 5, 4], [5, 4, 0]]
         ],
         [
             [[0, 1, 0], [1, 1, 1]],
             [[0, 1, 0], [1, 1, 1], [1, 0, 1]]
         ])
    y = [
        [[0, 0]],
        [[0, 1, 1]]
    ]
    d = WordTaggerDataset(X, y,
                          vocab_size=params['vocab_size'],
                          window_size=params['window_size'],
                          feat_num=params['feat_num'],
                          n_classes=params['n_classes'])
    return d, params


def test():
    data, params = test_data()
    st = SequenceTaggerNetwork(**params)
    st.create_algorithm(data)
    st.trainer.main_loop()


def init_brown():
    fn = sys.argv[1]
    featurizer = Featurizer()
    c = TaggedCorpus(fn, featurizer)
    d = WordTaggerDataset.create_from_tagged_corpus(c, window_size=6)
    wt = SequenceTaggerNetwork(vocab_size=d['train'].vocab_size,
                               window_size=d['train'].window_size,
                               total_feats=d['train'].total_feats,
                               feat_num=d['train'].feat_num,
                               n_classes=d['train'].n_classes,
                               edim=30, hdim=60, dataset=d['train'])
    return c, d, wt


def train_brown_pos():
    c, d, wt = init_brown()
    wt.create_algorithm(d, sys.argv[2])
    wt.trainer.main_loop()


def load_and_predict():
    c, d, _ = init_brown()
    wt = serial.load(sys.argv[2])
    print d['train'].y[0].argmax(axis=1)
    cost = SeqTaggerCost()
    words = T.matrix('words', dtype='int64')
    features = T.matrix('features', dtype='int64')
    targets = T.matrix('targets', dtype='float32')
    cost_expression = cost.compute_costs(wt, ((words, features), targets))
    fn = theano.function(inputs=[words, features, targets],
                         outputs=cost_expression)
    res = fn(d['train'].X1[0], d['train'].X2[0], d['train'].y[0])
    print res[1].argmax(), res[2].argmax(axis=1), res[3].argmax()


def predict_test():
    c, d, wt = init_brown()
    #print d['train'].y[0]
    #X = wt.get_input_space().make_theano_batch()
    #Y = wt.fprop(X)
    #f = theano.function([X[0], X[1]], Y)
    #y = f(d['train'].X1[0], d['train'].X2[0])
    #print y, y.shape
    #print (d['train'].y[0] * y).sum()
    #print len(d['train'].y[0])
    cost = SeqTaggerCost()
    words = T.matrix('words', dtype='int64')
    features = T.matrix('features', dtype='int64')
    targets = T.matrix('targets', dtype='float32')
    cost_expression = cost.expr(wt, ((words, features), targets))
    fn = theano.function(inputs=[words, features, targets],
                         outputs=cost_expression)
    print fn(d['train'].X1[0], d['train'].X2[0], d['train'].y[0])


if __name__ == "__main__":
    #predict_test()
    train_brown_pos()
    #load_and_predict()
