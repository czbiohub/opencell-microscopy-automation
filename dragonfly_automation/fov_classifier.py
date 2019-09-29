import os
import sys
import json
import skimage
import sklearn
import tifffile
import datetime
import numpy as np
import pandas as pd
from scipy import ndimage
from sklearn import cluster
from sklearn import metrics
from sklearn import ensemble
from skimage import feature
from skimage import morphology
from matplotlib import pyplot as plt

from dragonfly_automation import utils

def printr(s):
    sys.stdout.write('\r%s' % s)


def catch_errors(method):
    '''
    Wrapper for instance methods called in self.classify_raw_fov
    that catches and logs *all* exceptions and, if an exception occurs,
    calls self.make_decision to classify the FOV as 'not good'

    '''

    method_name = method.__name__

    def wrapper(self, *args, **kwargs):
        
        # only wrap in 'prediction' mode
        if self.mode == 'training':
            return method(self, *args, **kwargs)

        # do not call the method if an error has already occured,
        # or if the decision has already been made (if, e.g., the FOV was not a candidate)
        result = None
        if self.error_has_occured or self.decision_has_been_made:
            return result

        # attempt the method call
        try:
            result = method(self, *args, **kwargs)

        except Exception as error:
            error_info = dict(method_name=method_name, error_message=str(error))
            
            # make the classification decision, since we do not attempt any error recovery
            self.make_decision(
                decision=False, 
                reason=("Error in method `FOVClassifier.%s`" % method_name),
                error_info=error_info)

            # this breaks us out of the classification workflow by preventing
            # the execution of subsequent wrapped method calls (see above)
            self.error_has_occured = True

        return result

    return wrapper


class FOVClassifier:

    def __init__(self, cache_dir, log_dir=None, mode=None):
        '''
        cache_dir : str, required
            location to which to save the training data, if in training mode, 
            or from which to load existing training data, if in prediction mode
        log_dir : str, optional
            path to a local directory in which to save logfiles
        mode : 'training' or 'prediction'
        '''

        if mode not in ['training', 'prediction']:
            raise ValueError("`mode` must be either 'training' or 'prediction'")
        self.mode = mode

        os.makedirs(cache_dir, exist_ok=True)
        self.cache_dir = cache_dir

        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        self.log_dir = log_dir
        
        # optional external event logger assigned after instantiation
        self.external_event_logger = None

        # hard-coded image size
        self.image_size = 1024

        # hard-coded feature order for the classifier
        self.feature_order = (
            'num_nuclei',
            'com_offset',
            'eval_ratio',
            'total_area',
            'max_distance',
            'num_clusters',
            'num_unclustered',
        )

        # the classifier to use
        self.model = sklearn.ensemble.RandomForestClassifier(
            n_estimators=300,
            max_features='sqrt',
            oob_score=True)


    def training_data_filepath(self):
        return os.path.join(self.cache_dir, 'training_data.csv')

    def validation_results_filepath(self):
        return os.path.join(self.cache_dir, 'validation_results.json')


    def load(self):
        '''
        Load existing training data and validation results

        Steps
        1) load the training dataset and the cached cross-validation results
        2) train the classifier (self.model)
        3) verify that the cross-validation results are comparable to the cached results
        '''

        # reset the current validation results
        self.current_validation_results = None
    
        # load the training data        
        self.training_data = pd.read_csv(self.training_data_filepath())

        # load the cached validation results
        with open(self.validation_results_filepath(), 'r') as file:
            self.cached_validation_results = json.load(file)


    def save(self, overwrite=False):
        '''
        Save the training data and the validation results
        '''

        if self.mode != 'training':
            raise ValueError("Cannot save training data unless mode = 'training'")

        if self.current_validation_results is None:
            raise ValueError('Cannot save training data without current validation results')
        
        # don't overwrite existing data
        filepath = self.training_data_filepath()
        if os.path.isfile(filepath) and not overwrite:
            raise ValueError('Training data already saved to %s' % self.cache_dir)

        # save the training data
        self.training_data.to_csv(filepath, index=False)

        # save the validation results
        with open(self.validation_results_filepath(), 'w') as file:
            json.dump(self.current_validation_results, file)


    def process_training_data(self, data):
        '''
        Calculate the features from the training data images,
        and drop any images that are either not candidates or that yield errors

        Note that there is minimal error handling, since the training data has been curated
        and we should be able to find nuclei in every image without errors occurring

        Parameters
        ----------
        data : a pd.DataFrame with a 'filename' column and one or more label columns

        '''

        # create columns for the calculated features
        for feature_name in self.feature_order:
            data[feature_name] = None

        for ind, row in data.iterrows():
            printr(row.filename)
            im = tifffile.imread(row.filename)
            mask = self.generate_background_mask(im)
            positions = self.find_nucleus_positions(mask)
            fov_is_candidate = self.is_fov_candidate(positions)
            if fov_is_candidate:
                features = self.calculate_features(positions)
                for feature_name, feature_value in features.items():
                    data.at[ind, feature_name] = feature_value

        # drop rows with any missing/nan features
        mask = data[list(self.feature_order)].isna().sum(axis=1)
        if mask.sum():
            print('\nWarning: some training data was dropped; see self.dropped_data')
            self.dropped_data = data.loc[mask > 0]
            data = data.loc[mask == 0]
        self.training_data = data


    def train(self, label):
        '''
        Train and cross-validate a classifier to predict the given label
        
        Reminders:
            precision is (tp / (tp + fp))
            recall is (tp / (tp + fn))

        Parameters
        ----------
        label : the label to predict (must be a boolean column in self.training_data)
        '''

        # mask to identify training data with and without annotations
        # note that only the 'confluency' label is None if there is no annotation
        # (the other labels are False by default)
        mask = self.training_data['confluency'].isna()

        # training data with annotations (i.e., the 'real' training data)
        training_data = self.training_data.loc[~mask]
        X = training_data[list(self.feature_order)].values
        y = training_data[label].values.astype(bool)

        cv = sklearn.model_selection.StratifiedKFold(n_splits=10, shuffle=True)        
        scores = sklearn.model_selection.cross_validate(
            self.model, X, y, cv=cv, scoring=['accuracy', 'precision', 'recall'])

        validation_results = {key: '%0.2f' % value.mean() for key, value in scores.items()}

        # train the model on all of the training data
        self.model.fit(X, y)
        validation_results['full_oob_score'] = '%0.2f' % self.model.oob_score_
        self.current_validation_results = validation_results


    @catch_errors
    def predict(self, features):
        '''
        Use the pre-trained model to make a prediction
        '''

        # construct the feature array of shape (1, num_features)
        X = np.array([features.get(name) for name in self.feature_order])[None, :]
        prediction = self.model.predict(X)[0]
        return prediction


    def classify_raw_fov(self, image, position_ind=None):
        '''
        Classify a raw, uncurated FOV from the microscope itself

        Steps:
            validate the image object (yes, no, error)
            check that there are nuclei in the image (yes, no, error)
            generate the mask and find positions (continue, error)
            check the number of nuclei (yes, no, error)
            calculate features (continue, error)
            make the prediction (yes, no, error)

        Poorly documented failure modes:
          - if the image is random noise, then we usually get 'not a candidate'
          - if there is some giant but uniform artifact that doesn't break the thresholding
            (e.g., some large region is uniformly zero), we'll never know

        Parameters
        ----------
        image : numpy.ndarray (2D and uint16)
            The raw field of view to classify
        position_ind : int, optional (but required for logging)
            The index of the current position
            Note that we use an index, and not a label, because it's not clear
            that we can assume that the labels in the position list will always be unique 
            (e.g., they may not be when the position list is generated manually,
            rather than by the HCS Site Generator plugin)
        
        '''

        # reset the 'state' of the decision-making logic
        # TODO: more robust way of defining and resetting this 'state'
        self.error_has_occured = False
        self.decision_has_been_made = False

        # reset the log info
        # note that this dict is modified in this method *and* in self.make_decision
        self.log_info = {
            'position_ind': position_ind
        }

        # validate the image: check that it's a 2D uint16 ndarray
        # note that, because validate_raw_fov is wrapped by the catch_errors method,
        # we must check that the validation_result is not None before accessing the 'flag' key.
        # also note that we only include the image in self.log_info if it passes validation
        # (otherwise, errors may result when the image is later saved)
        validation_result = self.validate_raw_fov(image)
        if validation_result is not None:
            if validation_result.get('flag'):
                self.log_info['raw_image'] = image
            else:
                self.make_decision(decision=False, reason=validation_result.get('message'))
    
        # check whether there are any nuclei in the FOV
        nuclei_in_fov = self.are_nuclei_in_fov(image)
        if not nuclei_in_fov:
            self.make_decision(decision=False, reason='No nuclei in the FOV')

        # calculate the background mask
        mask = self.generate_background_mask(image)

        # calculate the nucleus positions from the mask
        positions = self.find_nucleus_positions(mask)
        self.log_info['positions'] = positions

        # determine if the FOV is a candidate
        fov_is_candidate = self.is_fov_candidate(positions)
        if not fov_is_candidate:
            self.make_decision(decision=False, reason='FOV is not a candidate')

        # calculate features from the positions
        features = self.calculate_features(positions)
        self.log_info['features'] = features

        # finally, use the trained model to generate a prediction
        # (True if the FOV is 'good')
        model_prediction = self.predict(features)
        self.make_decision(decision=model_prediction, reason='Model prediction')

        # log everything we've accumulated in self.log_info
        self.log_classification_info()
        return self.decision_flag


    def make_decision(self, decision, reason, error_info=None):
        '''
        '''
        # if an error has already occured, do nothing
        if self.error_has_occured or self.decision_has_been_made:
            return

        self.decision_flag = decision
        self.decision_has_been_made = True

        # update the log
        self.log_info['error_info'] = error_info
        self.log_info['decision'] = decision
        self.log_info['reason'] = reason


    def log_classification_info(self):
        '''
        '''
        
        log_info = self.log_info
        position_ind = log_info.get('position_ind')
        error_info = log_info.get('error_info')
        raw_image = log_info.get('raw_image')
        positions = log_info.get('positions')
        features = log_info.get('features')

        # if there's no log dir, we fall back to printing the decision and error (if any)
        if self.log_dir is None:
            if error_info is not None:
                print("Error during classification in method `%s`: '%s'" % \
                    (error_info.get('method_name'), error_info.get('error_message')))

            print("Classification decision: %s (reason: '%s')" % \
                (log_info.get('decision'), log_info.get('reason')))
            return

        # if there's an external event logger (presumably assigned by a program instance)
        if self.external_event_logger is not None:
            if log_info.get('decision'):
                message = "CLASSIFIER INFO: The FOV was accepted"
            else:
                message = "CLASSIFIER INFO: The FOV was rejected (reason: '%s')" % log_info.get('reason')
            self.external_event_logger(message)

        # if we're still here, we need a position_ind
        if position_ind is None:
            raise ValueError('A position_ind must be provided to log classification info')
        
        # directory and filepaths for logged images
        image_dir = os.path.join(self.log_dir, 'fov-images')
        os.makedirs(image_dir, exist_ok=True)
        def logged_image_filepath(tag):
            return os.path.join(image_dir, 'FOV_%05d_%s.tif' % (position_ind, tag))
        
        # construct the CSV log row
        row = {
            'position_ind': position_ind,
            'decision': log_info.get('decision'),
            'reason': log_info.get('reason'),
            'timestamp': utils.timestamp(),
            'image_filepath': logged_image_filepath('RAW'),
        }

        if error_info is not None:
            row.update(error_info)

        if features is not None:
            row.update(features)

        # append the row to the log file
        log_filepath = os.path.join(self.log_dir, 'fov-assessment.csv')
        if os.path.isfile(log_filepath):
            d = pd.read_csv(log_filepath)
            d = d.append(row, ignore_index=True)
        else:
            d = pd.DataFrame([row])
        d.to_csv(log_filepath, index=False, float_format='%0.2f')

        # log the raw image itself
        if raw_image is not None:
            tifffile.imsave(logged_image_filepath('RAW'), raw_image)

            # create a uint8 version 
            # (uint16 images can't be opened in Windows image preview)
            scaled_image = utils.to_uint8(raw_image)
            tifffile.imsave(logged_image_filepath('UINT8'), scaled_image)

            # create and save the annotated image
            # (in which nucleus positions are marked with white squares)
            if positions is not None:
                
                # spot width and whitepoint intensity
                width = 3
                white = 255
                sz = scaled_image.shape

                # lower the brightness of the autogained image 
                # so that the squares are easier to see
                ant_image = (scaled_image/2).astype('uint8')

                # draw a square on the image at each nucleus position
                for pos in positions:
                    ant_image[
                        int(max(0, pos[0] - width)):int(min(sz[0] - 1, pos[0] + width)), 
                        int(max(0, pos[1] - width)):int(min(sz[1] - 1, pos[1] + width))
                    ] = white

                tifffile.imsave(logged_image_filepath('ANT'), ant_image)



    @catch_errors
    def validate_raw_fov(self, image):
        
        flag = False
        message = None
        if not isinstance(image, np.ndarray):
            message = 'Image is not an np.ndarray'
        elif image.dtype != 'uint16':
            message = 'Image is not uint16'
        elif image.ndim != 2:
            message = 'Image is not 2D'
        elif image.shape[0] != self.image_size or image.shape[1] != self.image_size:
            message = 'Image shape is not (%s, %s)' % (self.image_size, self.image_size)
        else:
            flag = True
        
        return dict(flag=flag, message=message)


    @catch_errors
    def are_nuclei_in_fov(self, image):
        '''
        Check whether there are *any* real nuclei in the image

        This is accomplished by using an empirically-determine minimum Otsu threshold,
        which is predicated on the observation/assumption that that the background intensity 
        in raw FOVs is and will be always around 500.

        *** Note that this minimum value is sensitive to the exposure settings! ***
        (laser power, exposure time, camera gain, etc)
        '''
    
        min_otsu_thresh = 1000
        otsu_thresh = skimage.filters.threshold_li(image)
        nuclei_in_fov = otsu_thresh > min_otsu_thresh
        return nuclei_in_fov


    @catch_errors
    def is_fov_candidate(self, positions):
        '''
        Check whether there are way too few or way too many 'nuclei' in the FOV

        This will occur if either
           1) there are very few or very many real nuclei in the FOV, or
           2) there are _no_ real nuclei in the FOV,
              and the nucleus positions correspond to noise, dust, etc
              (we attempt to defend against this by first calling are_nuclei_in_fov)
        '''

        is_candidate = True
        min_num_nuclei = 10
        max_num_nuclei = 100

        num_positions = positions.shape[0]
        if num_positions < min_num_nuclei or num_positions > max_num_nuclei:
            is_candidate = False

        return is_candidate


    @catch_errors
    def generate_background_mask(self, image):

        # smooth the raw image
        imf = skimage.filters.gaussian(image, sigma=5)

        # background mask from minimum cross-entropy
        mask = imf > skimage.filters.threshold_li(imf)

        # erode once to eliminate isolated pixels in the mask
        # (a defense against thresholding noise)
        mask = skimage.morphology.erosion(mask)

        # remove regions that are too small to be nuclei
        min_region_area = 1000
        mask_label = skimage.measure.label(mask)
        props = skimage.measure.regionprops(mask_label)
        for prop in props:
            if prop.area < min_region_area:
                mask[mask_label==prop.label] = False

        return mask


    @catch_errors
    def find_nucleus_positions(self, mask):
        '''

        '''

        nucleus_radius = 15

        # smoothed distance transform
        dist = ndimage.distance_transform_edt(mask)
        distf = skimage.filters.gaussian(dist, sigma=1)

        # the positions of the local maximima in the distance transform
        # correspond roughly to the centers of mass of the individual nuclei
        positions = skimage.feature.peak_local_max(
            distf, indices=True, min_distance=nucleus_radius, labels=mask)

        return positions    


    @catch_errors
    def calculate_features(self, positions):
        '''
        '''

        features = {}

        # -------------------------------------------------------------------------
        #
        # Simple aggregate features of the nucleus positions
        # (center of mass and asymmetry) 
        #
        # -------------------------------------------------------------------------

        # the number of nuclei
        num_nuclei = positions.shape[0]
        
        # the distance of the center of mass from the center of the image
        com_offset = ((positions.mean(axis=0) - (self.image_size/2))**2).sum()**.5
        rel_com_offset = com_offset / self.image_size

        # eigenvalues of the covariance matrix
        evals, evecs = np.linalg.eig(np.cov(positions.transpose()))

        # the ratio of eigenvalues is a measure of asymmetry 
        eval_ratio = (max(evals) - min(evals))/min(evals)

        features.update({
            'num_nuclei': num_nuclei, 
            'com_offset': rel_com_offset, 
            'eval_ratio': eval_ratio,
        })

        # -------------------------------------------------------------------------
        #
        # Features derived from a simulated nucleus mask
        # (obtained by thresholding the distance transform of the nucleus positions)
        #
        # Note that it is necessary to simulate a nucleus mask,
        # rather than use the mask returned by _generate_background_mask, 
        # because the area of the foreground regions in the generated mask
        # depends on the focal plane.
        #
        # -------------------------------------------------------------------------

        # the nucleus radius here was selected empirically
        nucleus_radius = 50

        position_mask = np.zeros((self.image_size, self.image_size))
        for position in positions:
            position_mask[position[0], position[1]] = 1

        dt = ndimage.distance_transform_edt(~position_mask.astype(bool))
        total_area = (dt < nucleus_radius).sum() / (self.image_size*self.image_size)
        max_distance = dt.max()

        features.update({
            'total_area': total_area, 
            'max_distance': max_distance
        })

        # -------------------------------------------------------------------------
        #
        # Cluster positions using DBSCAN 
        # and calculate various measures of cluster homogeneity
        #
        # -------------------------------------------------------------------------
        
        # empirically-selected neighborhood size in pixels
        # (the clustering is *very* sensitive to changes in this parameter)
        eps = 100

        # min_samples = 3 is the minimum required for non-trivial clustering
        min_samples = 3

        dbscan = sklearn.cluster.DBSCAN(eps=eps, min_samples=min_samples, metric='euclidean')
        dbscan.fit(positions)

        labels = dbscan.labels_
        num_clusters = len(set(labels))
        num_unclustered = (labels==-1).sum()

        features.update({
            'num_clusters': num_clusters, 
            'num_unclustered': num_unclustered, 
        })

        return features



