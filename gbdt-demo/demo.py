
import numpy as np
from scipy import stats
from scipy.sparse import csc_matrix
from scipy.sparse import csr_matrix
from scipy.sparse import issparse
from scipy.special import expit
import six



class LogOddsEstimator(object):
    """An estimator predicting the log odds ratio."""
    scale = 1.0

    def fit(self, X, y, sample_weight=None):
        # pre-cond: pos, neg are encoded as 1, 0
        if sample_weight is None:
            pos = np.sum(y)
            neg = y.shape[0] - pos
        else:
            pos = np.sum(sample_weight * y)
            neg = np.sum(sample_weight * (1 - y))

        if neg == 0 or pos == 0:
            raise ValueError('y contains non binary labels.')
        self.prior = self.scale * np.log(pos / neg)

    def predict(self, X):

        y = np.empty((X.shape[0], 1), dtype=np.float64)
        y.fill(self.prior)
        return y


class LossFunction(six.with_metaclass(ABCMeta, object)):
    """Abstract base class for various loss functions.

    Attributes
    ----------
    K : int
        The number of regression trees to be induced;
        1 for regression and binary classification;
        ``n_classes`` for multi-class classification.
    """

    is_multi_class = False

    def __init__(self, n_classes):
        self.K = n_classes

    def init_estimator(self):
        """Default ``init`` estimator for loss function. """
        raise NotImplementedError()

    @abstractmethod
    def __call__(self, y, pred, sample_weight=None):
        """Compute the loss of prediction ``pred`` and ``y``. """

    @abstractmethod
    def negative_gradient(self, y, y_pred, **kargs):
        """Compute the negative gradient.

        Parameters
        ---------
        y : np.ndarray, shape=(n,)
            The target labels.
        y_pred : np.ndarray, shape=(n,):
            The predictions.
        """

    def update_terminal_regions(self, tree, X, y, residual, y_pred,
                                sample_weight, sample_mask,
                                learning_rate=1.0, k=0):
        """Update the terminal regions (=leaves) of the given tree and
        updates the current predictions of the model. Traverses tree
        and invokes template method `_update_terminal_region`.

        Parameters
        ----------
        tree : tree.Tree
            The tree object.
        X : ndarray, shape=(n, m)
            The data array.
        y : ndarray, shape=(n,)
            The target labels.
        residual : ndarray, shape=(n,)
            The residuals (usually the negative gradient).
        y_pred : ndarray, shape=(n,)
            The predictions.
        sample_weight : ndarray, shape=(n,)
            The weight of each sample.
        sample_mask : ndarray, shape=(n,)
            The sample mask to be used.
        learning_rate : float, default=0.1
            learning rate shrinks the contribution of each tree by
             ``learning_rate``.
        k : int, default 0
            The index of the estimator being updated.

        """
        # compute leaf for each sample in ``X``.
        terminal_regions = tree.apply(X)

        # mask all which are not in sample mask.
        masked_terminal_regions = terminal_regions.copy()
        masked_terminal_regions[~sample_mask] = -1

        # update each leaf (= perform line search)
        for leaf in np.where(tree.children_left == TREE_LEAF)[0]:
            self._update_terminal_region(tree, masked_terminal_regions,
                                         leaf, X, y, residual,
                                         y_pred[:, k], sample_weight)

        # update predictions (both in-bag and out-of-bag)
        y_pred[:, k] += (learning_rate
                         * tree.value[:, 0, 0].take(terminal_regions, axis=0))

    @abstractmethod
    def _update_terminal_region(self, tree, terminal_regions, leaf, X, y,
                                residual, pred, sample_weight):
        """Template method for updating terminal regions (=leaves). """


class BinomialDeviance(ClassificationLossFunction):
    """Binomial deviance loss function for binary classification.

    Binary classification is a special case; here, we only need to
    fit one tree instead of ``n_classes`` trees.
    """
    def __init__(self, n_classes):
        if n_classes != 2:
            raise ValueError("{0:s} requires 2 classes.".format(
                self.__class__.__name__))
        # we only need to fit one tree for binary clf.
        super(BinomialDeviance, self).__init__(1)

    def init_estimator(self):
        return LogOddsEstimator()

    def __call__(self, y, pred, sample_weight=None):
        """Compute the deviance (= 2 * negative log-likelihood). """
        # logaddexp(0, v) == log(1.0 + exp(v))
        pred = pred.ravel()
        if sample_weight is None:
            return -2.0 * np.mean((y * pred) - np.logaddexp(0.0, pred))
        else:
            return (-2.0 / sample_weight.sum() *
                    np.sum(sample_weight * ((y * pred) - np.logaddexp(0.0, pred))))

    def negative_gradient(self, y, pred, **kargs):
        """Compute the residual (= negative gradient). """
        return y - expit(pred.ravel())

    def _update_terminal_region(self, tree, terminal_regions, leaf, X, y,
                                residual, pred, sample_weight):
        """Make a single Newton-Raphson step.

        our node estimate is given by:

            sum(w * (y - prob)) / sum(w * prob * (1 - prob))

        we take advantage that: y - prob = residual
        """
        terminal_region = np.where(terminal_regions == leaf)[0]
        residual = residual.take(terminal_region, axis=0)
        y = y.take(terminal_region, axis=0)
        sample_weight = sample_weight.take(terminal_region, axis=0)

        numerator = np.sum(sample_weight * residual)
        denominator = np.sum(sample_weight * (y - residual) * (1 - y + residual))

        # prevents overflow and division by zero
        if abs(denominator) < 1e-150:
            tree.value[leaf, 0, 0] = 0.0
        else:
            tree.value[leaf, 0, 0] = numerator / denominator

    def _score_to_proba(self, score):
        proba = np.ones((score.shape[0], 2), dtype=np.float64)
        proba[:, 1] = expit(score.ravel())
        proba[:, 0] -= proba[:, 1]
        return proba

    def _score_to_decision(self, score):
        proba = self._score_to_proba(score)
        return np.argmax(proba, axis=1)

class ClassificationLossFunction(six.with_metaclass(ABCMeta, LossFunction)):
    """Base class for classification loss functions. """

    def _score_to_proba(self, score):
        """Template method to convert scores to probabilities.

         the does not support probabilities raises AttributeError.
        """
        raise TypeError('%s does not support predict_proba' % type(self).__name__)

    @abstractmethod
    def _score_to_decision(self, score):
        """Template method to convert scores to decisions.

        Returns int arrays.
        """


def accuracy_score(y_true, y_pred, normalize=True, sample_weight=None):

    # Compute accuracy for each possible representation
    sample_score = y_true == y_pred
    if normalize:
        return np.average(sample_score, weights=sample_weight)
    elif sample_weight is not None:
        return np.dot(sample_score, sample_weight)
    else:
        return sample_score.sum()

###############################################################################
class ClassifierMixin(object):
    _estimator_type = "classifier"
    def score(self, X, y, sample_weight=None):
        return accuracy_score(y, self.predict(X), sample_weight=sample_weight)


def _random_sample_mask(n_total_samples,
                        n_total_in_bag, random_state):
     rand = random_state.rand(n_total_samples)
     sample_mask = np.zeros((n_total_samples,), dtype=np.bool)

     n_bagged = 0
     i = 0

     for i in range(n_total_samples):
         if rand[i] * (n_total_samples - i) < (n_total_in_bag - n_bagged):
             sample_mask[i] = 1
             n_bagged += 1

     return sample_mask

class BaseGradientBoosting(six.with_metaclass(ABCMeta, BaseEnsemble)):
    """Abstract base class for Gradient Boosting. """

    @abstractmethod
    def __init__(self, loss, learning_rate, n_estimators, criterion,
                 min_samples_split, min_samples_leaf, min_weight_fraction_leaf,
                 max_depth, min_impurity_decrease, min_impurity_split,
                 init, subsample, max_features,
                 random_state, alpha=0.9, verbose=0, max_leaf_nodes=None,
                 warm_start=False, presort='auto'):

        self.n_estimators = n_estimators
        self.learning_rate = learning_rate
        self.loss = loss
        self.criterion = criterion
        self.min_samples_split = min_samples_split
        self.min_samples_leaf = min_samples_leaf
        self.min_weight_fraction_leaf = min_weight_fraction_leaf
        self.subsample = subsample
        self.max_features = max_features
        self.max_depth = max_depth
        self.min_impurity_decrease = min_impurity_decrease
        self.min_impurity_split = min_impurity_split
        self.init = init
        self.random_state = random_state
        self.alpha = alpha
        self.verbose = verbose
        self.max_leaf_nodes = max_leaf_nodes
        self.warm_start = warm_start
        self.presort = presort

    def _fit_stage(self, i, X, y, y_pred, sample_weight, sample_mask,
                   random_state, X_idx_sorted, X_csc=None, X_csr=None):
        """Fit another stage of ``n_classes_`` trees to the boosting model. """

        assert sample_mask.dtype == np.bool
        loss = self.loss_
        original_y = y

        for k in range(loss.K):
            if loss.is_multi_class:
                y = np.array(original_y == k, dtype=np.float64)

            residual = loss.negative_gradient(y, y_pred, k=k,
                                              sample_weight=sample_weight)

            # induce regression tree on residuals
            tree = DecisionTreeRegressor(
                criterion=self.criterion,
                splitter='best',
                max_depth=self.max_depth,
                min_samples_split=self.min_samples_split,
                min_samples_leaf=self.min_samples_leaf,
                min_weight_fraction_leaf=self.min_weight_fraction_leaf,
                min_impurity_decrease=self.min_impurity_decrease,
                min_impurity_split=self.min_impurity_split,
                max_features=self.max_features,
                max_leaf_nodes=self.max_leaf_nodes,
                random_state=random_state,
                presort=self.presort)

            if self.subsample < 1.0:
                # no inplace multiplication!
                sample_weight = sample_weight * sample_mask.astype(np.float64)

            if X_csc is not None:
                tree.fit(X_csc, residual, sample_weight=sample_weight,
                         check_input=False, X_idx_sorted=X_idx_sorted)
            else:
                tree.fit(X, residual, sample_weight=sample_weight,
                         check_input=False, X_idx_sorted=X_idx_sorted)

            # update tree leaves
            if X_csr is not None:
                loss.update_terminal_regions(tree.tree_, X_csr, y, residual, y_pred,
                                             sample_weight, sample_mask,
                                             self.learning_rate, k=k)
            else:
                loss.update_terminal_regions(tree.tree_, X, y, residual, y_pred,
                                             sample_weight, sample_mask,
                                             self.learning_rate, k=k)

            # add tree to ensemble
            self.estimators_[i, k] = tree

        return y_pred

    def _check_params(self):
        """Check validity of parameters and raise ValueError if not valid. """
        if self.n_estimators <= 0:
            raise ValueError("n_estimators must be greater than 0 but "
                             "was %r" % self.n_estimators)

        if self.learning_rate <= 0.0:
            raise ValueError("learning_rate must be greater than 0 but "
                             "was %r" % self.learning_rate)

        if (self.loss not in self._SUPPORTED_LOSS
                or self.loss not in LOSS_FUNCTIONS):
            raise ValueError("Loss '{0:s}' not supported. ".format(self.loss))

        if self.loss == 'deviance':
            loss_class = (MultinomialDeviance
                          if len(self.classes_) > 2
                          else BinomialDeviance)
        else:
            loss_class = LOSS_FUNCTIONS[self.loss]

        if self.loss in ('huber', 'quantile'):
            self.loss_ = loss_class(self.n_classes_, self.alpha)
        else:
            self.loss_ = loss_class(self.n_classes_)

        if not (0.0 < self.subsample <= 1.0):
            raise ValueError("subsample must be in (0,1] but "
                             "was %r" % self.subsample)

        if self.init is not None:
            if isinstance(self.init, six.string_types):
                if self.init not in INIT_ESTIMATORS:
                    raise ValueError('init="%s" is not supported' % self.init)
            else:
                if (not hasattr(self.init, 'fit')
                        or not hasattr(self.init, 'predict')):
                    raise ValueError("init=%r must be valid BaseEstimator "
                                     "and support both fit and "
                                     "predict" % self.init)

        if not (0.0 < self.alpha < 1.0):
            raise ValueError("alpha must be in (0.0, 1.0) but "
                             "was %r" % self.alpha)

        if isinstance(self.max_features, six.string_types):
            if self.max_features == "auto":
                # if is_classification
                if self.n_classes_ > 1:
                    max_features = max(1, int(np.sqrt(self.n_features_)))
                else:
                    # is regression
                    max_features = self.n_features_
            elif self.max_features == "sqrt":
                max_features = max(1, int(np.sqrt(self.n_features_)))
            elif self.max_features == "log2":
                max_features = max(1, int(np.log2(self.n_features_)))
            else:
                raise ValueError("Invalid value for max_features: %r. "
                                 "Allowed string values are 'auto', 'sqrt' "
                                 "or 'log2'." % self.max_features)
        elif self.max_features is None:
            max_features = self.n_features_
        elif isinstance(self.max_features, (numbers.Integral, np.integer)):
            max_features = self.max_features
        else:  # float
            if 0. < self.max_features <= 1.:
                max_features = max(int(self.max_features *
                                       self.n_features_), 1)
            else:
                raise ValueError("max_features must be in (0, n_features]")

        self.max_features_ = max_features

    def _init_state(self):
        """Initialize model state and allocate model state data structures. """

        if self.init is None:
            self.init_ = self.loss_.init_estimator()
        elif isinstance(self.init, six.string_types):
            self.init_ = INIT_ESTIMATORS[self.init]()
        else:
            self.init_ = self.init

        self.estimators_ = np.empty((self.n_estimators, self.loss_.K),
                                    dtype=np.object)
        self.train_score_ = np.zeros((self.n_estimators,), dtype=np.float64)
        # do oob?
        if self.subsample < 1.0:
            self.oob_improvement_ = np.zeros((self.n_estimators),
                                             dtype=np.float64)

    def _clear_state(self):
        """Clear the state of the gradient boosting model. """
        if hasattr(self, 'estimators_'):
            self.estimators_ = np.empty((0, 0), dtype=np.object)
        if hasattr(self, 'train_score_'):
            del self.train_score_
        if hasattr(self, 'oob_improvement_'):
            del self.oob_improvement_
        if hasattr(self, 'init_'):
            del self.init_

    def _resize_state(self):
        """Add additional ``n_estimators`` entries to all attributes. """
        # self.n_estimators is the number of additional est to fit
        total_n_estimators = self.n_estimators
        if total_n_estimators < self.estimators_.shape[0]:
            raise ValueError('resize with smaller n_estimators %d < %d' %
                             (total_n_estimators, self.estimators_[0]))

        self.estimators_.resize((total_n_estimators, self.loss_.K))
        self.train_score_.resize(total_n_estimators)
        if (self.subsample < 1 or hasattr(self, 'oob_improvement_')):
            # if do oob resize arrays or create new if not available
            if hasattr(self, 'oob_improvement_'):
                self.oob_improvement_.resize(total_n_estimators)
            else:
                self.oob_improvement_ = np.zeros((total_n_estimators,),
                                                 dtype=np.float64)

    def _is_initialized(self):
        return len(getattr(self, 'estimators_', [])) > 0

    def _check_initialized(self):
        """Check that the estimator is initialized, raising an error if not."""
        check_is_fitted(self, 'estimators_')

    @property
    @deprecated("Attribute n_features was deprecated in version 0.19 and "
                "will be removed in 0.21.")
    def n_features(self):
        return self.n_features_

    def fit(self, X, y, sample_weight=None, monitor=None):
        """Fit the gradient boosting model.

        Parameters
        ----------
        X : array-like, shape = [n_samples, n_features]
            Training vectors, where n_samples is the number of samples
            and n_features is the number of features.

        y : array-like, shape = [n_samples]
            Target values (integers in classification, real numbers in
            regression)
            For classification, labels must correspond to classes.

        sample_weight : array-like, shape = [n_samples] or None
            Sample weights. If None, then samples are equally weighted. Splits
            that would create child nodes with net zero or negative weight are
            ignored while searching for a split in each node. In the case of
            classification, splits are also ignored if they would result in any
            single class carrying a negative weight in either child node.

        monitor : callable, optional
            The monitor is called after each iteration with the current
            iteration, a reference to the estimator and the local variables of
            ``_fit_stages`` as keyword arguments ``callable(i, self,
            locals())``. If the callable returns ``True`` the fitting procedure
            is stopped. The monitor can be used for various things such as
            computing held-out estimates, early stopping, model introspect, and
            snapshoting.

        Returns
        -------
        self : object
            Returns self.
        """
        # if not warmstart - clear the estimator state
        if not self.warm_start:
            self._clear_state()

        # Check input
        X, y = check_X_y(X, y, accept_sparse=['csr', 'csc', 'coo'], dtype=DTYPE)
        n_samples, self.n_features_ = X.shape
        if sample_weight is None:
            sample_weight = np.ones(n_samples, dtype=np.float32)
        else:
            sample_weight = column_or_1d(sample_weight, warn=True)

        check_consistent_length(X, y, sample_weight)

        y = self._validate_y(y)

        random_state = check_random_state(self.random_state)
        self._check_params()

        if not self._is_initialized():
            # init state
            self._init_state()

            # fit initial model - FIXME make sample_weight optional
            self.init_.fit(X, y, sample_weight)

            # init predictions
            y_pred = self.init_.predict(X)
            begin_at_stage = 0
        else:
            # add more estimators to fitted model
            # invariant: warm_start = True
            if self.n_estimators < self.estimators_.shape[0]:
                raise ValueError('n_estimators=%d must be larger or equal to '
                                 'estimators_.shape[0]=%d when '
                                 'warm_start==True'
                                 % (self.n_estimators,
                                    self.estimators_.shape[0]))
            begin_at_stage = self.estimators_.shape[0]
            y_pred = self._decision_function(X)
            self._resize_state()

        X_idx_sorted = None
        presort = self.presort
        # Allow presort to be 'auto', which means True if the dataset is dense,
        # otherwise it will be False.
        if presort == 'auto' and issparse(X):
            presort = False
        elif presort == 'auto':
            presort = True

        if presort == True:
            if issparse(X):
                raise ValueError("Presorting is not supported for sparse matrices.")
            else:
                X_idx_sorted = np.asfortranarray(np.argsort(X, axis=0),
                                                 dtype=np.int32)

        # fit the boosting stages
        n_stages = self._fit_stages(X, y, y_pred, sample_weight, random_state,
                                    begin_at_stage, monitor, X_idx_sorted)
        # change shape of arrays after fit (early-stopping or additional ests)
        if n_stages != self.estimators_.shape[0]:
            self.estimators_ = self.estimators_[:n_stages]
            self.train_score_ = self.train_score_[:n_stages]
            if hasattr(self, 'oob_improvement_'):
                self.oob_improvement_ = self.oob_improvement_[:n_stages]

        return self

    def _fit_stages(self, X, y, y_pred, sample_weight, random_state,
                    begin_at_stage=0, monitor=None, X_idx_sorted=None):
        """Iteratively fits the stages.

        For each stage it computes the progress (OOB, train score)
        and delegates to ``_fit_stage``.
        Returns the number of stages fit; might differ from ``n_estimators``
        due to early stopping.
        """
        n_samples = X.shape[0]
        do_oob = self.subsample < 1.0
        sample_mask = np.ones((n_samples, ), dtype=np.bool)
        n_inbag = max(1, int(self.subsample * n_samples))
        loss_ = self.loss_

        # Set min_weight_leaf from min_weight_fraction_leaf
        if self.min_weight_fraction_leaf != 0. and sample_weight is not None:
            min_weight_leaf = (self.min_weight_fraction_leaf *
                               np.sum(sample_weight))
        else:
            min_weight_leaf = 0.

        if self.verbose:
            verbose_reporter = VerboseReporter(self.verbose)
            verbose_reporter.init(self, begin_at_stage)

        X_csc = csc_matrix(X) if issparse(X) else None
        X_csr = csr_matrix(X) if issparse(X) else None

        # perform boosting iterations
        i = begin_at_stage
        for i in range(begin_at_stage, self.n_estimators):

            # subsampling
            if do_oob:
                sample_mask = _random_sample_mask(n_samples, n_inbag,
                                                  random_state)
                # OOB score before adding this stage
                old_oob_score = loss_(y[~sample_mask],
                                      y_pred[~sample_mask],
                                      sample_weight[~sample_mask])

            # fit next stage of trees
            y_pred = self._fit_stage(i, X, y, y_pred, sample_weight,
                                     sample_mask, random_state, X_idx_sorted,
                                     X_csc, X_csr)

            # track deviance (= loss)
            if do_oob:
                self.train_score_[i] = loss_(y[sample_mask],
                                             y_pred[sample_mask],
                                             sample_weight[sample_mask])
                self.oob_improvement_[i] = (
                    old_oob_score - loss_(y[~sample_mask],
                                          y_pred[~sample_mask],
                                          sample_weight[~sample_mask]))
            else:
                # no need to fancy index w/ no subsampling
                self.train_score_[i] = loss_(y, y_pred, sample_weight)

            if self.verbose > 0:
                verbose_reporter.update(i, self)

            if monitor is not None:
                early_stopping = monitor(i, self, locals())
                if early_stopping:
                    break
        return i + 1

    def _make_estimator(self, append=True):
        # we don't need _make_estimator
        raise NotImplementedError()

    def _init_decision_function(self, X):
        """Check input and compute prediction of ``init``. """
        self._check_initialized()
        X = self.estimators_[0, 0]._validate_X_predict(X, check_input=True)
        if X.shape[1] != self.n_features_:
            raise ValueError("X.shape[1] should be {0:d}, not {1:d}.".format(
                self.n_features_, X.shape[1]))
        score = self.init_.predict(X).astype(np.float64)
        return score

    def _decision_function(self, X):
        # for use in inner loop, not raveling the output in single-class case,
        # not doing input validation.
        score = self._init_decision_function(X)
        predict_stages(self.estimators_, X, self.learning_rate, score)
        return score


    def _staged_decision_function(self, X):
        """Compute decision function of ``X`` for each iteration.

        This method allows monitoring (i.e. determine error on testing set)
        after each stage.

        Parameters
        ----------
        X : array-like or sparse matrix, shape = [n_samples, n_features]
            The input samples. Internally, it will be converted to
            ``dtype=np.float32`` and if a sparse matrix is provided
            to a sparse ``csr_matrix``.

        Returns
        -------
        score : generator of array, shape = [n_samples, k]
            The decision function of the input samples. The order of the
            classes corresponds to that in the attribute `classes_`.
            Regression and binary classification are special cases with
            ``k == 1``, otherwise ``k==n_classes``.
        """
        X = check_array(X, dtype=DTYPE, order="C",  accept_sparse='csr')
        score = self._init_decision_function(X)
        for i in range(self.estimators_.shape[0]):
            predict_stage(self.estimators_, i, X, self.learning_rate, score)
            yield score.copy()

    @property
    def feature_importances_(self):
        """Return the feature importances (the higher, the more important the
           feature).

        Returns
        -------
        feature_importances_ : array, shape = [n_features]
        """
        self._check_initialized()

        total_sum = np.zeros((self.n_features_, ), dtype=np.float64)
        for stage in self.estimators_:
            stage_sum = sum(tree.feature_importances_
                            for tree in stage) / len(stage)
            total_sum += stage_sum

        importances = total_sum / len(self.estimators_)
        return importances

    def _validate_y(self, y):
        self.n_classes_ = 1
        if y.dtype.kind == 'O':
            y = y.astype(np.float64)
        # Default implementation
        return y

    def apply(self, X):
        """Apply trees in the ensemble to X, return leaf indices.

        .. versionadded:: 0.17

        Parameters
        ----------
        X : array-like or sparse matrix, shape = [n_samples, n_features]
            The input samples. Internally, its dtype will be converted to
            ``dtype=np.float32``. If a sparse matrix is provided, it will
            be converted to a sparse ``csr_matrix``.

        Returns
        -------
        X_leaves : array_like, shape = [n_samples, n_estimators, n_classes]
            For each datapoint x in X and for each tree in the ensemble,
            return the index of the leaf x ends up in each estimator.
            In the case of binary classification n_classes is 1.
        """

        self._check_initialized()
        X = self.estimators_[0, 0]._validate_X_predict(X, check_input=True)

        # n_classes will be equal to 1 in the binary classification or the
        # regression case.
        n_estimators, n_classes = self.estimators_.shape
        leaves = np.zeros((X.shape[0], n_estimators, n_classes))

        for i in range(n_estimators):
            for j in range(n_classes):
                estimator = self.estimators_[i, j]
                leaves[:, i, j] = estimator.apply(X, check_input=False)

        return leaves




class GradientBoostingClassifier(BaseGradientBoosting, ClassifierMixin):

    _SUPPORTED_LOSS = ('deviance', 'exponential')

    def __init__(self, loss='deviance', learning_rate=0.1, n_estimators=100,
                 subsample=1.0, criterion='friedman_mse', min_samples_split=2,
                 min_samples_leaf=1, min_weight_fraction_leaf=0.,
                 max_depth=3, min_impurity_decrease=0.,
                 min_impurity_split=None, init=None,
                 random_state=None, max_features=None, verbose=0,
                 max_leaf_nodes=None, warm_start=False,
                 presort='auto'):

        super(GradientBoostingClassifier, self).__init__(
            loss=loss, learning_rate=learning_rate, n_estimators=n_estimators,
            criterion=criterion, min_samples_split=min_samples_split,
            min_samples_leaf=min_samples_leaf,
            min_weight_fraction_leaf=min_weight_fraction_leaf,
            max_depth=max_depth, init=init, subsample=subsample,
            max_features=max_features,
            random_state=random_state, verbose=verbose,
            max_leaf_nodes=max_leaf_nodes,
            min_impurity_decrease=min_impurity_decrease,
            min_impurity_split=min_impurity_split,
            warm_start=warm_start,
            presort=presort)


    def decision_function(self, X):
        """Compute the decision function of ``X``.

        Parameters
        ----------
        X : array-like or sparse matrix, shape = [n_samples, n_features]
            The input samples. Internally, it will be converted to
            ``dtype=np.float32`` and if a sparse matrix is provided
            to a sparse ``csr_matrix``.

        Returns
        -------
        score : array, shape = [n_samples, n_classes] or [n_samples]
            The decision function of the input samples. The order of the
            classes corresponds to that in the attribute `classes_`.
            Regression and binary classification produce an array of shape
            [n_samples].
        """
        X = check_array(X, dtype=DTYPE, order="C",  accept_sparse='csr')
        score = self._decision_function(X)
        if score.shape[1] == 1:
            return score.ravel()
        return score

    def staged_decision_function(self, X):
        """Compute decision function of ``X`` for each iteration.

        This method allows monitoring (i.e. determine error on testing set)
        after each stage.

        Parameters
        ----------
        X : array-like or sparse matrix, shape = [n_samples, n_features]
            The input samples. Internally, it will be converted to
            ``dtype=np.float32`` and if a sparse matrix is provided
            to a sparse ``csr_matrix``.

        Returns
        -------
        score : generator of array, shape = [n_samples, k]
            The decision function of the input samples. The order of the
            classes corresponds to that in the attribute `classes_`.
            Regression and binary classification are special cases with
            ``k == 1``, otherwise ``k==n_classes``.
        """
        for dec in self._staged_decision_function(X):
            # no yield from in Python2.X
            yield dec

    def predict(self, X):
        """Predict class for X.

        Parameters
        ----------
        X : array-like or sparse matrix, shape = [n_samples, n_features]
            The input samples. Internally, it will be converted to
            ``dtype=np.float32`` and if a sparse matrix is provided
            to a sparse ``csr_matrix``.

        Returns
        -------
        y : array of shape = [n_samples]
            The predicted values.
        """
        score = self.decision_function(X)
        decisions = self.loss_._score_to_decision(score)
        return self.classes_.take(decisions, axis=0)

    def staged_predict(self, X):
        """Predict class at each stage for X.

        This method allows monitoring (i.e. determine error on testing set)
        after each stage.

        Parameters
        ----------
        X : array-like or sparse matrix, shape = [n_samples, n_features]
            The input samples. Internally, it will be converted to
            ``dtype=np.float32`` and if a sparse matrix is provided
            to a sparse ``csr_matrix``.

        Returns
        -------
        y : generator of array of shape = [n_samples]
            The predicted value of the input samples.
        """
        for score in self._staged_decision_function(X):
            decisions = self.loss_._score_to_decision(score)
            yield self.classes_.take(decisions, axis=0)

    def predict_proba(self, X):
        """Predict class probabilities for X.

        Parameters
        ----------
        X : array-like or sparse matrix, shape = [n_samples, n_features]
            The input samples. Internally, it will be converted to
            ``dtype=np.float32`` and if a sparse matrix is provided
            to a sparse ``csr_matrix``.

        Raises
        ------
        AttributeError
            If the ``loss`` does not support probabilities.

        Returns
        -------
        p : array of shape = [n_samples]
            The class probabilities of the input samples. The order of the
            classes corresponds to that in the attribute `classes_`.
        """
        score = self.decision_function(X)
        try:
            return self.loss_._score_to_proba(score)
        except NotFittedError:
            raise
        except AttributeError:
            raise AttributeError('loss=%r does not support predict_proba' %
                                 self.loss)

    def predict_log_proba(self, X):
        """Predict class log-probabilities for X.

        Parameters
        ----------
        X : array-like or sparse matrix, shape = [n_samples, n_features]
            The input samples. Internally, it will be converted to
            ``dtype=np.float32`` and if a sparse matrix is provided
            to a sparse ``csr_matrix``.

        Raises
        ------
        AttributeError
            If the ``loss`` does not support probabilities.

        Returns
        -------
        p : array of shape = [n_samples]
            The class log-probabilities of the input samples. The order of the
            classes corresponds to that in the attribute `classes_`.
        """
        proba = self.predict_proba(X)
        return np.log(proba)

    def staged_predict_proba(self, X):
        """Predict class probabilities at each stage for X.

        This method allows monitoring (i.e. determine error on testing set)
        after each stage.

        Parameters
        ----------
        X : array-like or sparse matrix, shape = [n_samples, n_features]
            The input samples. Internally, it will be converted to
            ``dtype=np.float32`` and if a sparse matrix is provided
            to a sparse ``csr_matrix``.

        Returns
        -------
        y : generator of array of shape = [n_samples]
            The predicted value of the input samples.
        """
        try:
            for score in self._staged_decision_function(X):
                yield self.loss_._score_to_proba(score)
        except NotFittedError:
            raise
        except AttributeError:
            raise AttributeError('loss=%r does not support predict_proba' %
                                 self.loss)[
