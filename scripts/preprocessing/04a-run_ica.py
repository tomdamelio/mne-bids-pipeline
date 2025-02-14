"""
===========
04. Run ICA
===========
This fits ICA on epoched data filtered with 1 Hz highpass,
for this purpose only using fastICA. Separate ICAs are fitted and stored for
MEG and EEG data.

To actually remove designated ICA components from your data, you will have to
run 05a-apply_ica.py.
"""

import itertools
import logging
from tqdm import tqdm

import pandas as pd
import numpy as np

import mne
from mne.report import Report
from mne.preprocessing import ICA, create_ecg_epochs, create_eog_epochs
from mne.parallel import parallel_func

from mne_bids import BIDSPath

import config
from config import gen_log_message, on_error, failsafe_run

logger = logging.getLogger('mne-bids-pipeline')


def load_and_concatenate_raws(bids_path):
    subject = bids_path.subject
    session = bids_path.session
    raws = []
    for run in config.get_runs():
        raw_fname_in = bids_path.copy().update(run=run, processing='filt',
                                               suffix='raw', check=False)

        if raw_fname_in.copy().update(split='01').fpath.exists():
            raw_fname_in.update(split='01')

        msg = f'Loading filtered raw data from {raw_fname_in}'
        logger.info(gen_log_message(message=msg, step=4, subject=subject,
                                    session=session, run=run))

        raw = mne.io.read_raw_fif(raw_fname_in, preload=False)
        raws.append(raw)

    msg = 'Concatenating runs'
    logger.info(gen_log_message(message=msg, step=4, subject=subject,
                                session=session))

    if len(raws) == 1:  # avoid extra memory usage
        raw = raws[0]
    else:
        raw = mne.concatenate_raws(raws)
    del raws

    raw.load_data()  # Load before setting EEG reference

    if "eeg" in config.ch_types:
        projection = True if config.eeg_reference == 'average' else False
        raw.set_eeg_reference(config.eeg_reference, projection=projection)

    return raw


def filter_for_ica(raw, subject, session):
    """Apply a high-pass filter if needed."""
    if config.ica_l_freq <= config.l_freq or config.ica_l_freq is None:
        # Nothing to do here!
        msg = 'Not applying high-pass filter '
        if config.ica_l_freq <= config.l_freq:
            msg += (f'(data is already filtered, '
                    f'cutoff: {raw.info["highpass"]} Hz).')
        else:
            msg = '(no filtering requested).'
        logger.info(gen_log_message(message=msg, step=4, subject=subject,
                                    session=session))
    else:
        msg = f'Applying high-pass filter with {config.ica_l_freq} Hz cutoff …'
        logger.info(gen_log_message(message=msg, step=4, subject=subject,
                                    session=session))
        raw.filter(l_freq=config.ica_l_freq, h_freq=None)

    return raw


def make_epochs_for_ica(raw, subject, session):
    """Epoch the raw data, and equalize epoch selection with step 3."""

    # First, load the existing epochs. We will extract the selection of kept
    # epochs.
    epochs_fname = BIDSPath(subject=subject,
                            session=session,
                            task=config.get_task(),
                            acquisition=config.acq,
                            recording=config.rec,
                            space=config.space,
                            suffix='epo',
                            extension='.fif',
                            datatype=config.get_datatype(),
                            root=config.deriv_root,
                            check=False)
    epochs = mne.read_epochs(epochs_fname)
    selection = epochs.selection

    # Now, create new epochs, and only keep the ones we kept in step 3.
    # Because some events present in event_id may disappear entirely from the
    # data, we pass `on_missing='ignore'` to mne.Epochs. Also note that we do
    # not pass the `reject` parameter here.

    events, event_id = mne.events_from_annotations(raw)
    events = events[selection]
    epochs_ica = mne.Epochs(raw, events=events, event_id=event_id,
                            tmin=epochs.tmin, tmax=epochs.tmax,
                            baseline=None,
                            on_missing='ignore',
                            decim=config.decim, proj=True, preload=True)

    return epochs_ica


def fit_ica(epochs, subject, session):
    if config.ica_algorithm == 'picard':
        fit_params = dict(fastica_it=5)
    elif config.ica_algorithm == 'extended_infomax':
        fit_params = dict(extended=True)
    elif config.ica_algorithm == 'fastica':
        fit_params = None

    ica = ICA(method=config.ica_algorithm, random_state=config.random_state,
              n_components=config.ica_n_components, fit_params=fit_params,
              max_iter=config.ica_max_iterations)

    ica.fit(epochs, decim=config.ica_decim)

    explained_var = (ica.pca_explained_variance_[:ica.n_components_].sum() /
                     ica.pca_explained_variance_.sum())
    msg = (f'Fit {ica.n_components_} components (explaining '
           f'{round(explained_var * 100, 1)}% of the variance) in '
           f'{ica.n_iter_} iterations.')
    logger.info(gen_log_message(message=msg, step=4, subject=subject,
                                session=session))
    return ica


def detect_ecg_artifacts(ica, raw, subject, session, report):
    # ECG either needs an ecg channel, or avg of the mags (i.e. MEG data)
    if ('ecg' in raw.get_channel_types() or 'meg' in config.ch_types or
            'mag' in config.ch_types):
        msg = 'Performing automated ECG artifact detection …'
        logger.info(gen_log_message(message=msg, step=4, subject=subject,
                                    session=session))

        # Do not reject epochs based on amplitude.
        ecg_epochs = create_ecg_epochs(raw, reject=None,
                                       baseline=(None, -0.2),
                                       tmin=-0.5, tmax=0.5)
        ecg_evoked = ecg_epochs.average()
        ecg_inds, scores = ica.find_bads_ecg(
            ecg_epochs, method='ctps',
            threshold=config.ica_ctps_ecg_threshold)
        ica.exclude = ecg_inds

        msg = (f'Detected {len(ecg_inds)} ECG-related ICs in '
               f'{len(ecg_epochs)} ECG epochs.')
        logger.info(gen_log_message(message=msg, step=4, subject=subject,
                                    session=session))
        del ecg_epochs

        # Plot scores
        fig = ica.plot_scores(scores, labels='ecg', show=config.interactive)
        report.add_figs_to_section(figs=fig, captions='Scores - ECG',
                                   section=f'sub-{subject}')

        # Plot source time course
        fig = ica.plot_sources(ecg_evoked, show=config.interactive)
        report.add_figs_to_section(figs=fig,
                                   captions='Source time course - ECG',
                                   section=f'sub-{subject}')

        # Plot original & corrected data
        fig = ica.plot_overlay(ecg_evoked, show=config.interactive)
        report.add_figs_to_section(figs=fig, captions='Corrections - ECG',
                                   section=f'sub-{subject}')
    else:
        ecg_inds = list()
        msg = ('No ECG or magnetometer channels are present. Cannot '
               'automate artifact detection for ECG')
        logger.info(gen_log_message(message=msg, step=4, subject=subject,
                                    session=session))

    return ecg_inds


def detect_eog_artifacts(ica, raw, subject, session, report):
    pick_eog = mne.pick_types(raw.info, meg=False, eeg=False, ecg=False,
                              eog=True)
    if config.eog_channels:
        assert all([ch_name in raw.ch_names
                    for ch_name in config.eog_channels])
        ch_name = ','.join(config.eog_channels)
    else:
        ch_name = None

    if pick_eog.any() or config.eog_channels:
        msg = 'Performing automated EOG artifact detection …'
        logger.info(gen_log_message(message=msg, step=4, subject=subject,
                                    session=session))

        # Do not reject epochs based on amplitude.
        eog_epochs = create_eog_epochs(raw, ch_name=ch_name, reject=None,
                                       baseline=(None, -0.2),
                                       tmin=-0.5, tmax=0.5)
        eog_evoked = eog_epochs.average()
        eog_inds, scores = ica.find_bads_eog(
            eog_epochs,
            threshold=config.ica_eog_threshold)
        ica.exclude = eog_inds

        msg = (f'Detected {len(eog_inds)} EOG-related ICs in '
               f'{len(eog_epochs)} EOG epochs.')
        logger.info(gen_log_message(message=msg, step=4, subject=subject,
                                    session=session))
        del eog_epochs

        # Plot scores
        fig = ica.plot_scores(scores, labels='eog', show=config.interactive)
        report.add_figs_to_section(figs=fig, captions='Scores - EOG',
                                   section=f'sub-{subject}')

        # Plot source time course
        fig = ica.plot_sources(eog_evoked, show=config.interactive)
        report.add_figs_to_section(figs=fig,
                                   captions='Source time course - EOG',
                                   section=f'sub-{subject}')

        # Plot original & corrected data
        fig = ica.plot_overlay(eog_evoked, show=config.interactive)
        report.add_figs_to_section(figs=fig, captions='Corrections - EOG',
                                   section=f'sub-{subject}')
    else:
        eog_inds = list()
        msg = ('No EOG channel is present. Cannot automate IC detection '
               'for EOG')
        logger.info(gen_log_message(message=msg, step=4, subject=subject,
                                    session=session))

    return eog_inds


def run_ica(subject, session=None):
    """Run ICA."""
    bids_basename = BIDSPath(subject=subject,
                             session=session,
                             task=config.get_task(),
                             acquisition=config.acq,
                             recording=config.rec,
                             space=config.space,
                             datatype=config.get_datatype(),
                             root=config.deriv_root,
                             check=False)

    ica_fname = bids_basename.copy().update(suffix='ica', extension='.fif')
    ica_components_fname = bids_basename.copy().update(processing='ica',
                                                       suffix='components',
                                                       extension='.tsv')
    report_fname = bids_basename.copy().update(processing='ica',
                                               suffix='report',
                                               extension='.html')

    msg = 'Loading and concatenating filtered continuous "raw" data'
    logger.info(gen_log_message(message=msg, step=4, subject=subject,
                                session=session))
    raw = load_and_concatenate_raws(bids_basename.copy().update(
        processing='filt', suffix='raw', extension='.fif'))

    # Sanity check – make sure we're using the correct data!
    if config.resample_sfreq is not None:
        np.testing.assert_allclose(raw.info['sfreq'], config.resample_sfreq)
    if config.l_freq is not None:
        np.testing.assert_allclose(raw.info['highpass'], config.l_freq)

    # Produce high-pass filtered version of the data for ICA.
    # filter_for_ica will concatenate all runs of our raw data.
    # We don't have to worry about edge artifacts due to raw concatenation as
    # we'll be epoching the data in the next step.
    raw = filter_for_ica(raw, subject=subject, session=session)
    epochs = make_epochs_for_ica(raw, subject=subject, session=session)

    # Now actually perform ICA.
    msg = 'Calculating ICA solution.'
    logger.info(gen_log_message(message=msg, step=4, subject=subject,
                                session=session))
    report = Report(info_fname=raw,
                    title='Independent Component Analysis (ICA)',
                    verbose=False)
    ica = fit_ica(epochs, subject=subject, session=session)
    ecg_ics = detect_ecg_artifacts(ica=ica, raw=raw, subject=subject,
                                   session=session, report=report)
    eog_ics = detect_eog_artifacts(ica=ica, raw=raw, subject=subject,
                                   session=session, report=report)

    # Save ICA to disk.
    # We also store the automatically identified ECG- and EOG-related ICs.
    msg = 'Saving ICA solution and detected artifacts to disk.'
    logger.info(gen_log_message(message=msg, step=4, subject=subject,
                                session=session))
    ica.exclude = sorted(set(ecg_ics + eog_ics))
    ica.save(ica_fname)

    # Create TSV.
    tsv_data = pd.DataFrame(
        dict(component=list(range(ica.n_components_)),
             type=['ica'] * ica.n_components_,
             description=['Independent Component'] * ica.n_components_,
             status=['good'] * ica.n_components_,
             status_description=['n/a'] * ica.n_components_))

    for component in ecg_ics:
        row_idx = tsv_data['component'] == component
        tsv_data.loc[row_idx, 'status'] = 'bad'
        tsv_data.loc[row_idx,
                     'status_description'] = 'Auto-detected ECG artifact'

    for component in eog_ics:
        row_idx = tsv_data['component'] == component
        tsv_data.loc[row_idx, 'status'] = 'bad'
        tsv_data.loc[row_idx,
                     'status_description'] = 'Auto-detected EOG artifact'

    tsv_data.to_csv(ica_components_fname, sep='\t', index=False)

    # Lastly, plot all ICs, and add them to the report for manual inspection.
    msg = 'Adding diagnostic plots for all ICs to the HTML report …'
    logger.info(gen_log_message(message=msg, step=4, subject=subject,
                                session=session))
    for component_num in tqdm(range(ica.n_components_)):
        fig = ica.plot_properties(epochs,
                                  picks=component_num,
                                  psd_args={'fmax': 60},
                                  show=False)

        caption = f'IC {component_num}'
        if component_num in eog_ics and component_num in ecg_ics:
            caption += ' (EOG & ECG)'
        elif component_num in eog_ics:
            caption += ' (EOG)'
        elif component_num in ecg_ics:
            caption += ' (ECG)'
        report.add_figs_to_section(fig, section=f'sub-{subject}',
                                   captions=caption)

    open_browser = True if config.interactive else False
    report.save(report_fname, overwrite=True, open_browser=open_browser)

    msg = (f"ICA completed. Please carefully review the extracted ICs in the "
           f"report {report_fname.basename}, and mark all components you wish "
           f"to reject as 'bad' in {ica_components_fname.basename}")
    logger.info(gen_log_message(message=msg, step=4, subject=subject,
                                session=session))


@failsafe_run(on_error=on_error)
def main():
    """Run ICA."""
    msg = 'Running Step 4: Compute ICA'
    logger.info(gen_log_message(step=4, message=msg))

    if config.use_ica:
        parallel, run_func, _ = parallel_func(run_ica, n_jobs=config.N_JOBS)
        parallel(run_func(subject, session) for subject, session in
                 itertools.product(config.get_subjects(),
                                   config.get_sessions()))

    msg = 'Completed Step 4: Compute ICA'
    logger.info(gen_log_message(step=4, message=msg))


if __name__ == '__main__':
    main()
