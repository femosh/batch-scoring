import argparse
import copy
import logging
import os
import sys
import warnings
from multiprocessing import freeze_support

from datarobot_batch_scoring import __version__
from datarobot_batch_scoring.batch_scoring import (run_batch_predictions)
from datarobot_batch_scoring.writer import ShelveError
from datarobot_batch_scoring.utils import (UI, get_config_file,
                                           parse_config_file,
                                           verify_objectid,
                                           parse_host)

VERSION_TEMPLATE = '%(prog)s {}'.format(__version__)


DESCRIPTION = """
Batch score DATASET by submitting prediction requests against HOST
using PROJECT_ID and MODEL_ID (or IMPORT_ID in case of standalone predictions).

It optimizes prediction throughput by sending data in batches of 1.5mb.

Set N_CONCURRENT to match the number of cores in the prediction API endpoint.

The dataset has to be a single CSV file that can be gzipped (extension '.gz').
The OUT will be a single CSV file but remember that records
might be unordered.
"""


EPILOG = """
Example:
  $ batch_scoring --host https://example.orm.datarobot.com \
  --user="<username>" --password="<password>" 5545eb20b4912911244d4835 \
  5545eb71b4912911244d4847 ~/Downloads/diabetes_test.csv
"""


VALID_DELIMITERS = {';', ',', '|', '\t', ' ', '!', '  '}


def parse_args(argv, standalone=False):
    parser = argparse.ArgumentParser(
        description=DESCRIPTION, epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--verbose', '-v', action="store_true",
                        help='Provides status updates while '
                        'the script is running.')
    parser.add_argument('--version', action='version',
                        version=VERSION_TEMPLATE, help='Show version')
    dataset_gr = parser.add_argument_group('Dataset and server')
    dataset_gr.add_argument('--host', type=str,
                            help='Specifies the protocol (http or https) and '
                                 'hostname of the prediction API endpoint. '
                                 'E.g. "https://example.orm.datarobot.com"')
    dataset_gr.add_argument('--out', type=str,
                            nargs='?', default='out.csv',
                            help='Specifies the file name, '
                            'and optionally path, '
                            'to which the results are written. '
                            'If not specified, '
                            'the default file name is out.csv, '
                            'written to the directory containing the script. '
                            '(default: %(default)r)')
    if standalone:
        dataset_gr.add_argument('import_id', type=str,
                                help='Specifies the project '
                                'identification string.')
    else:
        dataset_gr.add_argument('project_id', type=str,
                                help='Specifies the project '
                                'identification string.')
        dataset_gr.add_argument('model_id', type=str,
                                help='Specifies the model identification '
                                     'string.')
        auth_gr = parser.add_argument_group('Authentication parameters')
        auth_gr.add_argument('--user', type=str,
                             help='Specifies the username used to acquire '
                             'the api-token. '
                             'Use quotes if the name contains spaces.')
        auth_gr.add_argument('--password', type=str, nargs='?',
                             help='Specifies the password used to acquire '
                             'the api-token. '
                             'Use quotes if the name contains spaces.')
        auth_gr.add_argument('--api_token', type=str, nargs='?',
                             help='Specifies the api token for the requests; '
                             'if you do not have a token, '
                             'you must specify the password argument.')
        auth_gr.add_argument('--create_api_token', action="store_true",
                             default=False,
                             help='Requests a new API token. To use this '
                                  'option, you must specify the '
                                  'password argument for this request '
                                  '(not the api_token argument). '
                                  '(default: %(default)r)')
        auth_gr.add_argument('--datarobot_key', type=str,
                             nargs='?',
                             help='An additional datarobot_key '
                             'for dedicated prediction instances.')
    dataset_gr.add_argument('dataset', type=str,
                            help='Specifies the .csv input file that '
                            'the script scores.')

    conn_gr = parser.add_argument_group('Connection control')
    conn_gr.add_argument('--timeout', type=int,
                         default=None,
                         help='The timeout for each post request. '
                         '(default: %(default)r)')
    conn_gr.add_argument('--n_samples', type=int,
                         nargs='?',
                         default=False,
                         help='DEPRECATED. Specifies the number of samples '
                              '(rows) to use per batch. If not defined the '
                              '"auto_sample" option will be used.')
    conn_gr.add_argument('--n_concurrent', type=int,
                         nargs='?',
                         default=4,
                         help='Specifies the number of concurrent requests '
                         'to submit. (default: %(default)r)')
    conn_gr.add_argument('--n_retry', type=int,
                         default=3,
                         help='Specifies the number of times DataRobot '
                         'will retry if a request fails. '
                         'A value of -1 specifies an infinite '
                         'number of retries. (default: %(default)r)')
    conn_gr.add_argument('--resume', action='store_true',
                         default=False,
                         help='Starts the prediction from the point at which '
                         'it was halted. '
                         'If the prediction stopped, for example due '
                         'to error or network connection issue, you can run '
                         'the same command with all the same '
                         'all arguments plus this resume argument.')
    conn_gr.add_argument('--compress', action='store_true',
                         default=False,
                         help='Compress batch. This can improve throughout '
                              'when bandwidth is limited.')
    csv_gr = parser.add_argument_group('CVS parameters')
    csv_gr.add_argument('--keep_cols', type=str,
                        nargs='?',
                        help='Specifies the column names to append '
                        'to the predictions. '
                        'Enter as a comma-separated list.')
    csv_gr.add_argument('--delimiter', type=str,
                        nargs='?', default=None,
                        help='Specifies the delimiter to recognize in '
                        'the input .csv file. E.g. "--delimiter=,". '
                        'If not specified, the script tries to automatically '
                        'determine the delimiter. The special keyword "tab" '
                        'can be used to indicate a tab delimited csv. "pipe"'
                        'can be used to indicate "|"')
    csv_gr.add_argument('--pred_name', type=str,
                        nargs='?', default=None,
                        help='Specifies column name for prediction results, '
                        'empty name is used if not specified. For binary '
                        'predictions assumes last class in lexical order '
                        'as positive')
    csv_gr.add_argument('--fast', action='store_true',
                        default=False,
                        help='Experimental: faster CSV processor. '
                        'Note: does not support multiline csv. ')
    csv_gr.add_argument('--auto_sample', action='store_true',
                        default=False,
                        help='Override "n_samples" and instead '
                        'use chunks of about 1.5 MB. This is recommended and '
                        'enabled by default if "n_samples" is not defined.')
    csv_gr.add_argument('--encoding', type=str,
                        default='', help='Declare the dataset encoding. '
                        'If an encoding is not provided the batch_scoring '
                        'script attempts to detect it. E.g "utf-8", "latin-1" '
                        'or "iso2022_jp". See the Python docs for a list of '
                        'valid encodings '
                        'https://docs.python.org/3/library/codecs.html'
                        '#standard-encodings')
    csv_gr.add_argument('--skip_dialect',  action='store_true',
                        default=False, help='Tell the batch_scoring script '
                        'to skip csv dialect detection.')
    csv_gr.add_argument('--skip_row_id', action='store_true', default=False,
                        help='Skip the row_id column in output.')
    csv_gr.add_argument('--output_delimiter', type=str, default=None,
                        help='Set the delimiter for output file.The special '
                             'keyword "tab" can be used to indicate a tab '
                             'delimited csv. "pipe" can be used to indicate '
                             '"|"')
    misc_gr = parser.add_argument_group('Miscellaneous')
    misc_gr.add_argument('-y', '--yes', dest='prompt', action='store_true',
                         help="Always answer 'yes' for user prompts")
    misc_gr.add_argument('-n', '--no', dest='prompt', action='store_false',
                         help="Always answer 'no' for user prompts")
    misc_gr.add_argument('--dry_run', dest='dry_run', action='store_true',
                         help="Only read/chunk input data but dont send "
                         "requests.")
    misc_gr.add_argument('--stdout', action='store_true', dest='stdout',
                         default=False,
                         help='Send all log messages to stdout.')

    defaults = {
        'prompt': None,
        'out': 'out.csv',
        'create_api_token': False,
        'timeout': 30,
        'n_samples': False,
        'n_concurrent': 4,
        'n_retry': 3,
        'resume': False,
        'fast': False,
        'stdout': False,
        'auto_sample': False,
    }
    conf_file = get_config_file()
    if conf_file:
        file_args = parse_config_file(conf_file)
        defaults.update(file_args)
    parser.set_defaults(**defaults)
    for action in parser._actions:
        if action.dest in defaults and action.required:
            action.required = False
            if '--' + action.dest not in argv:
                action.nargs = '?'
    parsed_args = {k: v
                   for k, v in vars(parser.parse_args(argv)).items()
                   if v is not None}
    return parsed_args


def parse_generic_options(parsed_args):
    global ui
    loglevel = logging.DEBUG if parsed_args['verbose'] else logging.INFO
    stdout = parsed_args['stdout']
    ui = UI(parsed_args.get('prompt'), loglevel, stdout)

    printed_args = copy.copy(parsed_args)
    printed_args.pop('password', None)
    ui.debug(printed_args)
    ui.info('platform: {} {}'.format(sys.platform, sys.version))
    n_retry = int(parsed_args['n_retry'])
    if parsed_args.get('keep_cols'):
        keep_cols = [s.strip() for s in parsed_args['keep_cols'].split(',')]
    else:
        keep_cols = None
    concurrent = int(parsed_args['n_concurrent'])

    resume = parsed_args['resume']
    compression = parsed_args['compress']
    out_file = parsed_args['out']
    timeout = int(parsed_args['timeout'])
    fast_mode = parsed_args['fast']
    encoding = parsed_args['encoding']
    skip_dialect = parsed_args['skip_dialect']
    skip_row_id = parsed_args['skip_row_id']
    host = parsed_args.get('host')
    pred_name = parsed_args.get('pred_name')
    dry_run = parsed_args.get('dry_run', False)
    base_url = ""

    n_samples = int(parsed_args['n_samples'])
    auto_sample = parsed_args['auto_sample']
    if not n_samples:
        auto_sample = True

    delimiter = parsed_args.get('delimiter')
    if delimiter == '\\t' or delimiter == 'tab':
        # NOTE: on bash you have to use Ctrl-V + TAB
        delimiter = '\t'
    elif delimiter == 'pipe':
        # using the | char has issues on Windows for some reason
        delimiter = '|'
    if delimiter and delimiter not in VALID_DELIMITERS:
        ui.fatal('Delimiter "{}" is not a valid delimiter.'
                 .format(delimiter))

    output_delimiter = parsed_args.get('output_delimiter')
    if output_delimiter == '\\t' or output_delimiter == 'tab':
        # NOTE: on bash you have to use Ctrl-V + TAB
        output_delimiter = '\t'
    elif output_delimiter == 'pipe':
        output_delimiter = '|'
    if output_delimiter and output_delimiter not in VALID_DELIMITERS:
        ui.fatal('Output delimiter "{}" is not a valid delimiter.'
                 .format(output_delimiter))

    dataset = parsed_args['dataset']
    if not os.path.exists(dataset):
        ui.fatal('file {} does not exist.'.format(dataset))

    if not dry_run:
        base_url = parse_host(host, ui)

    ui.debug('batch_scoring v{}'.format(__version__))
    ui.info('connecting to {}'.format(base_url))

    return {
        'auto_sample': auto_sample,
        'base_url': base_url,
        'compression': compression,
        'concurrent': concurrent,
        'dataset': dataset,
        'delimiter': delimiter,
        'dry_run': dry_run,
        'encoding': encoding,
        'fast_mode': fast_mode,
        'keep_cols': keep_cols,
        'n_retry': n_retry,
        'n_samples': n_samples,
        'out_file': out_file,
        'output_delimiter': output_delimiter,
        'pred_name': pred_name,
        'resume': resume,
        'skip_dialect': skip_dialect,
        'skip_row_id': skip_row_id,
        'timeout': timeout,
    }


def main(argv=sys.argv[1:]):
    freeze_support()
    global ui  # global variable hack, will get rid of a bit later
    warnings.simplefilter('ignore')
    parsed_args = parse_args(argv)
    exit_code = 1

    generic_opts = parse_generic_options(parsed_args)

    # parse args
    pid = parsed_args['project_id']
    lid = parsed_args['model_id']
    try:
        verify_objectid(pid)
        verify_objectid(lid)
    except ValueError as e:
        ui.fatal(str(e))

    # auth only ---
    datarobot_key = parsed_args.get('datarobot_key')
    api_token = parsed_args.get('api_token')
    create_api_token = parsed_args.get('create_api_token')
    user = parsed_args.get('user')
    pwd = parsed_args.get('password')

    if not generic_opts['dry_run']:
        user = user or ui.prompt_user()
        user = user.strip()

        if not api_token and not pwd:
            pwd = ui.getpass()

    base_headers = {}
    if datarobot_key:
        base_headers['datarobot-key'] = datarobot_key
    # end auth ---

    try:
        exit_code = run_batch_predictions(
            base_headers=base_headers, user=user, pwd=pwd,
            api_token=api_token, create_api_token=create_api_token,
            pid=pid, lid=lid, import_id=None, ui=ui, **generic_opts
        )
    except SystemError:
        pass
    except ShelveError as e:
        ui.error(str(e))
    except KeyboardInterrupt:
        ui.info('Keyboard interrupt')
    except Exception as e:
        ui.fatal(str(e))
    finally:
        ui.close()
        return exit_code


def main_standalone(argv=sys.argv[1:]):
    freeze_support()
    global ui  # global variable hack, will get rid of a bit later
    warnings.simplefilter('ignore')
    parsed_args = parse_args(argv, standalone=True)
    exit_code = 1

    generic_opts = parse_generic_options(parsed_args)
    import_id = parsed_args['import_id']
    try:
        exit_code = run_batch_predictions(
            base_headers={}, user=None, pwd=None,
            api_token=None, create_api_token=False,
            pid=None, lid=None, import_id=import_id, ui=ui, **generic_opts
        )
    except SystemError:
        pass
    except ShelveError as e:
        ui.error(str(e))
    except KeyboardInterrupt:
        ui.info('Keyboard interrupt')
    except Exception as e:
        ui.fatal(str(e))
    finally:
        ui.close()
        return exit_code


if __name__ == '__main__':
    exit_code = main()  # pragma: no cover
    sys.exit(exit_code)
