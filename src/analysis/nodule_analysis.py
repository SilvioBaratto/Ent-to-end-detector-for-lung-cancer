import argparse
import glob
import os
import sys

import numpy as np
import scipy.ndimage.measurements as measurements
import scipy.ndimage.morphology as morphology

import torch
import torch.nn as nn
import torch.optim

import sys
sys.path.append("..")

from torch.utils.data import DataLoader

from util.util import enumerateWithEstimate
from dataset.dsets_segmentation import Luna2dSegmentationDataset
from dataset.dsets_classification import LunaDataset, getCt, getCandidateInfoDict, getCandidateInfoList, CandidateInfoTuple
from model.model_segmentation import UNetWrapper

import model.model_classification as model

from util.logconf import logging
from util.util import xyz2irc, irc2xyz

log = logging.getLogger(__name__)
# log.setLevel(logging.WARN)
# log.setLevel(logging.INFO)
log.setLevel(logging.DEBUG)
logging.getLogger("dsets_segmentation").setLevel(logging.WARNING)
logging.getLogger("dsets_classification").setLevel(logging.WARNING)

def print_confusion(label, confusions, do_mal):
    row_labels = ['Non-Nodules', 'Benign', 'Malignant']

    if do_mal:
        col_labels = ['', 'Complete Miss', 'Filtered Out', 'Pred. Benign', 'Pred. Malignant']
    else:
        col_labels = ['', 'Complete Miss', 'Filtered Out', 'Pred. Nodule']
        confusions[:, -2] += confusions[:, -1]
        confusions = confusions[:, :-1]
    cell_width = 16
    f = '{:>' + str(cell_width) + '}'
    print(label)
    print(' | '.join([f.format(s) for s in col_labels]))
    for i, (l, r) in enumerate(zip(row_labels, confusions)):
        r = [l] + list(r)
        if i == 0:
            r[1] = ''
        print(' | '.join([f.format(i) for i in r]))

def match_and_score(detections, truth, threshold=0.5, threshold_mal=0.5):
    # Returns 3x4 confusion matrix for:
    # Rows: Truth: Non-Nodules, Benign, Malignant
    # Cols: Not Detected, Detected by Seg, Detected as Benign, Detected as Malignant
    # If one true nodule matches multiple detections, the "highest" detection is considered
    # If one detection matches several true nodule annotations, it counts for all of them
    true_nodules = [c for c in truth if c.isNodule_bool]
    truth_diams = np.array([c.diameter_mm for c in true_nodules])
    truth_xyz = np.array([c.center_xyz for c in true_nodules])

    detected_xyz = np.array([n[2] for n in detections])
    # detection classes will contain
    # 1 -> detected by seg but filtered by cls
    # 2 -> detected as benign nodule (or nodule if no malignancy model is used)
    # 3 -> detected as malignant nodule (if applicable)
    detected_classes = np.array([1 if d[0] < threshold
                                 else (2 if d[1] < threshold
                                       else 3) for d in detections])

    confusion = np.zeros((3, 4), dtype=np.int)
    if len(detected_xyz) == 0:
        for tn in true_nodules:
            confusion[2 if tn.isMal_bool else 1, 0] += 1
    elif len(truth_xyz) == 0:
        for dc in detected_classes:
            confusion[0, dc] += 1
    else:
        normalized_dists = np.linalg.norm(truth_xyz[:, None] - detected_xyz[None], ord=2, axis=-1) / truth_diams[:, None]
        matches = (normalized_dists < 0.7)
        unmatched_detections = np.ones(len(detections), dtype=np.bool)
        matched_true_nodules = np.zeros(len(true_nodules), dtype=np.int)
        for i_tn, i_detection in zip(*matches.nonzero()):
            matched_true_nodules[i_tn] = max(matched_true_nodules[i_tn], detected_classes[i_detection])
            unmatched_detections[i_detection] = False

        for ud, dc in zip(unmatched_detections, detected_classes):
            if ud:
                confusion[0, dc] += 1
        for tn, dc in zip(true_nodules, matched_true_nodules):
            confusion[2 if tn.isMal_bool else 1, dc] += 1
    return confusion

class NoduleAnalysisApp:
    def __init__(self, sys_argv=None):
        if sys_argv is None:
            log.debug(sys.argv)
            sys_argv = sys.argv[1:]

        parser = argparse.ArgumentParser()
        parser.add_argument('--batch-size',
            help='Batch size to use for training',
            default=4,
            type=int,
        )
        parser.add_argument('--num-workers',
            help='Number of worker processes for background data loading',
            default=48,
            type=int,
        )

        parser.add_argument('--run-validation',
            help='Run over validation rather than a single CT.',
            action='store_true',
            default=False,
        )
        parser.add_argument('--include-train',
            help="Include data that was in the training set. (default: validation data only)",
            action='store_true',
            default=False,
        )

        parser.add_argument('--segmentation-path',
            help="Path to the saved segmentation model",
            nargs='?',
            default='../../models/segmentation/seg_2022-07-21_17.29.23_final_seg.best.state',
        )

        parser.add_argument('--cls-model',
            help="What to model class name to use for the classifier.",
            action='store',
            default='LunaModel',
        )
        parser.add_argument('--classification-path',
            help="Path to the saved classification model",
            nargs='?',
            default='../../models/classification/cls_2022-07-21_19.09.18_nodule-nonnodule.best.state',
        )

        parser.add_argument('--malignancy-model',
            help="What to model class name to use for the malignancy classifier.",
            action='store',
            default='LunaModel',
            # default='ModifiedLunaModel',
        )
        parser.add_argument('--malignancy-path',
            help="Path to the saved malignancy classification model",
            nargs='?',
            default='../../models/classification/cls_2022-07-22_10.25.28_malben-finetune-twolayer.best.state',
        )

        parser.add_argument('--tb-prefix',
            default='nodule_analysis',
            help="Data prefix to use for Tensorboard run. Defaults to chapter.",
        )

        parser.add_argument('series_uid',
            nargs='?',
            default=None,
            help="Series UID to use.",
        )

        self.cli_args = parser.parse_args(sys_argv)
        # self.time_str = datetime.datetime.now().strftime('%Y-%m-%d_%H:%M:%S')

        if not (bool(self.cli_args.series_uid) ^ self.cli_args.run_validation):
            raise Exception("One and only one of series_uid and --run-validation should be given")


        self.use_cuda = torch.cuda.is_available()
        self.device = torch.device("cuda" if self.use_cuda else "cpu")

        if not self.cli_args.segmentation_path:
            self.cli_args.segmentation_path = self.initModelPath('seg')

        if not self.cli_args.classification_path:
            self.cli_args.classification_path = self.initModelPath('cls')

        self.seg_model, self.cls_model, self.malignancy_model = self.initModels()

    def initModelPath(self, type_str):
        local_path = os.path.join(
            '..',
            '..',
            'models',
            type_str + '_{}_{}.{}.state'.format('*', '*', 'best'),
        )

        file_list = glob.glob(local_path)
        if not file_list:
            pretrained_path = os.path.join(
                '..',
                '..',
                'models',
                type_str + '_{}_{}.{}.state'.format('*', '*', '*'),
            )
            file_list = glob.glob(pretrained_path)
        else:
            pretrained_path = None

        file_list.sort()

        try:
            return file_list[-1]
        except IndexError:
            log.debug([local_path, pretrained_path, file_list])
            raise

    def initModels(self):
        log.debug(self.cli_args.segmentation_path)
        seg_dict = torch.load(self.cli_args.segmentation_path)

        seg_model = UNetWrapper(
            in_channels=7,
            n_classes=1,
            depth=3,
            wf=4,
            padding=True,
            batch_norm=True,
            up_mode='upconv',
        )

        seg_model.load_state_dict(seg_dict['model_state'])
        seg_model.eval()

        log.debug(self.cli_args.classification_path)
        cls_dict = torch.load(self.cli_args.classification_path)

        model_cls = getattr(model, self.cli_args.cls_model)
        cls_model = model_cls()
        cls_model.load_state_dict(cls_dict['model_state'])
        cls_model.eval()

        if self.use_cuda:
            if torch.cuda.device_count() > 1:
                seg_model = nn.DataParallel(seg_model)
                cls_model = nn.DataParallel(cls_model)

            seg_model.to(self.device)
            cls_model.to(self.device)

        if self.cli_args.malignancy_path:
            model_cls = getattr(model, self.cli_args.malignancy_model)
            malignancy_model = model_cls()
            malignancy_dict = torch.load(self.cli_args.malignancy_path)
            malignancy_model.load_state_dict(malignancy_dict['model_state'])
            malignancy_model.eval()
            if self.use_cuda:
                malignancy_model.to(self.device)
        else:
            malignancy_model = None
        return seg_model, cls_model, malignancy_model


    def initSegmentationDl(self, series_uid):
        seg_ds = Luna2dSegmentationDataset(
                contextSlices_count=3,
                series_uid=series_uid,
                fullCt_bool=True,
            )
        seg_dl = DataLoader(
            seg_ds,
            batch_size=self.cli_args.batch_size * (torch.cuda.device_count() if self.use_cuda else 1),
            num_workers=self.cli_args.num_workers,
            pin_memory=self.use_cuda,
        )

        return seg_dl

    def initClassificationDl(self, candidateInfo_list):
        cls_ds = LunaDataset(
                sortby_str='series_uid',
                candidateInfo_list=candidateInfo_list,
            )
        cls_dl = DataLoader(
            cls_ds,
            batch_size=self.cli_args.batch_size * (torch.cuda.device_count() if self.use_cuda else 1),
            num_workers=self.cli_args.num_workers,
            pin_memory=self.use_cuda,
        )

        return cls_dl


    def main(self):
        log.info("Starting {}, {}".format(type(self).__name__, self.cli_args))

        val_ds = LunaDataset(
            val_stride=10,
            isValSet_bool=True,
        )
        val_set = set(
            candidateInfo_tup.series_uid
            for candidateInfo_tup in val_ds.candidateInfo_list
        )
        positive_set = set(
            candidateInfo_tup.series_uid
            for candidateInfo_tup in getCandidateInfoList()
            if candidateInfo_tup.isNodule_bool
        )

        if self.cli_args.series_uid:
            series_set = set(self.cli_args.series_uid.split(','))
        else:
            series_set = set(
                candidateInfo_tup.series_uid
                for candidateInfo_tup in getCandidateInfoList()
            )

        if self.cli_args.include_train:
            train_list = sorted(series_set - val_set)
        else:
            train_list = []
        val_list = sorted(series_set & val_set)


        candidateInfo_dict = getCandidateInfoDict()
        series_iter = enumerateWithEstimate(
            val_list + train_list,
            "Series",
        )
        all_confusion = np.zeros((3, 4), dtype=np.int)
        for _, series_uid in series_iter:   # Loops over the series UIDs
            ct = getCt(series_uid)  # Gets the CT 
            mask_a = self.segmentCt(ct, series_uid) # Runs our segmentation model on it

            candidateInfo_list = self.groupSegmentationOutput(  # Group the flagged voxels in the output
                series_uid, ct, mask_a)
            classifications_list = self.classifyCandidates( # Runs our nodule classifier on them
                ct, candidateInfo_list)

            if not self.cli_args.run_validation:
                print(f"found nodule candidates in {series_uid}:")
                for prob, prob_mal, center_xyz, center_irc in classifications_list:
                    if prob > 0.5:  # ... for all candidates found by the segmentation where the classifier assigned a
                                    # nodule probability of 50% or more. 
                        s = f"nodule prob {prob:.3f}, "
                        if self.malignancy_model:
                            s += f"malignancy prob {prob_mal:.3f}, "
                        s += f"center xyz {center_xyz}"
                        print(s)

            if series_uid in candidateInfo_dict:    # If we have the ground truth data, we compute and print the confusion 
                                                    # matrix and also add the current results to the total
                one_confusion = match_and_score(
                    classifications_list, candidateInfo_dict[series_uid]
                )
                all_confusion += one_confusion
                print_confusion(
                    series_uid, one_confusion, self.malignancy_model is not None
                )

        print_confusion(
            "Total", all_confusion, self.malignancy_model is not None
        )


    def classifyCandidates(self, ct, candidateInfo_list):
        cls_dl = self.initClassificationDl(candidateInfo_list)  # Again, we get a dataloader to loop over, this time 
                                                                # based on our candidate list.
        classifications_list = []
        for batch_ndx, batch_tup in enumerate(cls_dl):
            input_t, _, _, series_list, center_list = batch_tup

            input_g = input_t.to(self.device)   # Sends the inputs to the device
            with torch.no_grad():
                _, probability_nodule_g = self.cls_model(input_g)   # Runs the inputs through the nodule vs non-nodule network
                if self.malignancy_model is not None:   # If we have a malignancy model, we run that, too.
                    _, probability_mal_g = self.malignancy_model(input_g)
                else:
                    probability_mal_g = torch.zeros_like(probability_nodule_g)

            zip_iter = zip(center_list,
                probability_nodule_g[:,1].tolist(),
                probability_mal_g[:,1].tolist())
            for center_irc, prob_nodule, prob_mal in zip_iter:  # Does our bookkeping, constructing a list of our     
                                                                # results
                center_xyz = irc2xyz(center_irc,
                    direction_a=ct.direction_a,
                    origin_xyz=ct.origin_xyz,
                    vxSize_xyz=ct.vxSize_xyz,
                )
                cls_tup = (prob_nodule, prob_mal, center_xyz, center_irc)
                classifications_list.append(cls_tup)
        return classifications_list

    def segmentCt(self, ct, series_uid):
        with torch.no_grad():   # We do not need gradients here, so we don't build the graph
            output_a = np.zeros_like(ct.hu_a, dtype=np.float32) # This array will hold our output: a float
                                                                # array of probability annotations
            
            seg_dl = self.initSegmentationDl(series_uid)    # We get a dataloader that lets us loop over our
                                                            # Ct in batches
            for input_t, _, _, slice_ndx_list in seg_dl:

                input_g = input_t.to(self.device)   # After moving the input to the GPU ... 
                prediction_g = self.seg_model(input_g)  # .. We run segmentation model ...

                for i, slice_ndx in enumerate(slice_ndx_list):  # ... And copy each element to the output array.
                    output_a[slice_ndx] = prediction_g[i].cpu().numpy()

            mask_a = output_a > 0.5 # Threshold the probability outputs to get a binary output, and the applies
                                    # binary erosion as cleanup

            mask_a = morphology.binary_erosion(mask_a, iterations=1)

        return mask_a

    def groupSegmentationOutput(self, series_uid,  ct, clean_a):
        candidateLabel_a, candidate_count = measurements.label(clean_a) # Assign each voxel the label of the group it belongs to

        centerIrc_list = measurements.center_of_mass(   # Gets the center of mass for each for each group as index, row, column
                                                        # coordinates
            ct.hu_a.clip(-1000, 1000) + 1001,
            labels=candidateLabel_a,
            index=np.arange(1, candidate_count+1),
        )

        candidateInfo_list = []
        for i, center_irc in enumerate(centerIrc_list):
            center_xyz = irc2xyz(   # Converts the voxel coordinates to real patient coordinates
                center_irc,
                ct.origin_xyz,
                ct.vxSize_xyz,
                ct.direction_a,
            )
            assert np.all(np.isfinite(center_irc)), repr(['irc', center_irc, i, candidate_count])
            assert np.all(np.isfinite(center_xyz)), repr(['xyz', center_xyz])
            candidateInfo_tup = \
                CandidateInfoTuple(False, False, False, 0.0, series_uid, center_xyz)    # Build our candidate info tuple and
                                                                                        # appends it to the list of detections
            candidateInfo_list.append(candidateInfo_tup)

        return candidateInfo_list

    def logResults(self, mode_str, filtered_list, series2diagnosis_dict, positive_set):
        count_dict = {'tp': 0, 'tn': 0, 'fp': 0, 'fn': 0}
        for series_uid in filtered_list:
            probablity_float, center_irc = series2diagnosis_dict.get(series_uid, (0.0, None))
            if center_irc is not None:
                center_irc = tuple(int(x.item()) for x in center_irc)
            positive_bool = series_uid in positive_set
            prediction_bool = probablity_float > 0.5
            correct_bool = positive_bool == prediction_bool

            if positive_bool and prediction_bool:
                count_dict['tp'] += 1
            if not positive_bool and not prediction_bool:
                count_dict['tn'] += 1
            if not positive_bool and prediction_bool:
                count_dict['fp'] += 1
            if positive_bool and not prediction_bool:
                count_dict['fn'] += 1


            log.info("{} {} Label:{!r:5} Pred:{!r:5} Correct?:{!r:5} Value:{:.4f} {}".format(
                mode_str,
                series_uid,
                positive_bool,
                prediction_bool,
                correct_bool,
                probablity_float,
                center_irc,
            ))

        total_count = sum(count_dict.values())
        percent_dict = {k: v / (total_count or 1) * 100 for k, v in count_dict.items()}

        precision = percent_dict['p'] = count_dict['tp'] / ((count_dict['tp'] + count_dict['fp']) or 1)
        recall    = percent_dict['r'] = count_dict['tp'] / ((count_dict['tp'] + count_dict['fn']) or 1)
        percent_dict['f1'] = 2 * (precision * recall) / ((precision + recall) or 1)

        log.info(mode_str + " tp:{tp:.1f}%, tn:{tn:.1f}%, fp:{fp:.1f}%, fn:{fn:.1f}%".format(
            **percent_dict,
        ))
        log.info(mode_str + " precision:{p:.3f}, recall:{r:.3f}, F1:{f1:.3f}".format(
            **percent_dict,
        ))



if __name__ == '__main__':
    NoduleAnalysisApp().main()
