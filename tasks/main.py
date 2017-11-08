'''Main task entrypoint'''

import os
import io

from datetime import datetime

from invoke import task
from stopit import TimeoutException, SignalTimeout as Timeout

from .common import load_dotenv
from log_utils import logging
from log_utils.remote_logs import (
    post_output_log, post_build_complete,
    post_build_error, post_build_timeout)

# TODO: somehow also send the logger outputs to the log callback

LOGGER = logging.getLogger('MAIN')
TIMEOUT_SECONDS = 20 * 60  # 20 minutes


def replace_private_values(text, private_values,
                           replace_string='[PRIVATE VALUE HIDDEN]'):
    '''
    Replaces instances of the strings in `private_values` list
    with `replace_string`


    >>> replace_private_values('18F boop Rulez boop', ['boop'])
    '18F [PRIVATE VALUE HIDDEN] Rulez [PRIVATE VALUE HIDDEN]'

    >>> replace_private_values('18F boop Rulez beep',
    ...                        ['boop', 'beep'], 'STUFF')
    '18F STUFF Rulez STUFF'
    '''
    for priv_val in private_values:
        text = text.replace(priv_val, replace_string)

    return text


def run_task(ctx, task_name, log_callback_url, flags_dict=None, env=None):
    '''
    Uses `ctx.run` to call the specified `task_name` with the
    argument flags given in `flags_dict` and `env`, if specified.
    '''
    flag_args = []
    if flags_dict:
        for flag, val in flags_dict.items():
            flag_args.append(f"{flag}='{val}'")

    command = f'inv {task_name} {" ".join(flag_args)}'

    run_kwargs = {}
    if env:
        run_kwargs['env'] = env

    result = ctx.run(command, **run_kwargs)

    post_output_log(log_callback_url=log_callback_url,
                    source=task_name,
                    output=result.stdout)

    return result


@task
def main(ctx):
    '''
    Main task to run a full site build process.

    All values needed for the build are loaded from
    environment variables.
    '''
    # (variable naming)
    # pylint: disable=C0103

    # keep track of total time
    start_time = datetime.now()

    LOGGER.info('Running full build process')

    # Load from .env for development
    load_dotenv()

    # These environment variables will be set into the environment
    # by federalist-builder.

    # During development, we can use a `.env` file (loaded above)
    # to make it easier to specify variables.

    AWS_DEFAULT_REGION = os.environ['AWS_DEFAULT_REGION']
    BUCKET = os.environ['BUCKET']
    BASEURL = os.environ['BASEURL']
    CACHE_CONTROL = os.environ['CACHE_CONTROL']
    BRANCH = os.environ['BRANCH']
    CONFIG = os.environ['CONFIG']
    REPOSITORY = os.environ['REPOSITORY']
    OWNER = os.environ['OWNER']
    SITE_PREFIX = os.environ['SITE_PREFIX']
    GENERATOR = os.environ['GENERATOR']

    AWS_ACCESS_KEY_ID = os.environ['AWS_ACCESS_KEY_ID']
    AWS_SECRET_ACCESS_KEY = os.environ['AWS_SECRET_ACCESS_KEY']
    GITHUB_TOKEN = os.environ['GITHUB_TOKEN']

    # Optional environment variables
    SOURCE_REPO = os.getenv('SOURCE_REPO')
    SOURCE_OWNER = os.getenv('SOURCE_OWNER')

    # Ex: https://federalist-builder.fr.cloud.gov/builds/<token>/callback
    FEDERALIST_BUILDER_CALLBACK = os.environ['FEDERALIST_BUILDER_CALLBACK']

    # Ex: https://federalist-staging.18f.gov/v0/build/<build_id>/status/<token>
    STATUS_CALLBACK = os.environ['STATUS_CALLBACK']

    # Ex: https://federalist-staging.18f.gov/v0/build/<build_id>/log/<token>
    LOG_CALLBACK = os.environ['LOG_CALLBACK']

    try:
        # throw a timeout exception after TIMEOUT_SECONDS
        with Timeout(TIMEOUT_SECONDS, swallow_exc=False):

            # Unfortunately, pyinvoke doesn't have a great way to call tasks from
            # within other tasks. If you call them directly, their pre- and post-
            # dependencies are not executed.
            #
            # Here's the GitHub issue about this:
            # https://github.com/pyinvoke/invoke/issues/170
            #
            # So rather than calling the task functions through python, we'll
            # call them instead using `ctx.run('invoke the_task ...')` via the
            # helper `run_task` method.

            ##
            # CLONE
            #
            if SOURCE_OWNER and SOURCE_REPO:
                # First clone the source (ie, template) repository
                clone_source_flags = {
                    '--owner': SOURCE_OWNER,
                    '--repository': SOURCE_REPO,
                    '--branch': BRANCH,
                }

                run_task(ctx, 'clone-repo', LOG_CALLBACK,
                         flags_dict=clone_source_flags,
                         env={'GITHUB_TOKEN': GITHUB_TOKEN})

                # Then push the cloned source repo up to the destination repo.
                # Note that the destination repo must already exist but be empty.
                # The Federalist web app takes care of that operation.
                push_repo_flags = {
                    '--owner': OWNER,
                    '--repository': REPOSITORY,
                    '--branch': BRANCH,
                }

                run_task(ctx, 'push-repo-remote', LOG_CALLBACK,
                         flags_dict=push_repo_flags,
                         env={'GITHUB_TOKEN': GITHUB_TOKEN})
            else:
                # Just clone the given repository
                clone_flags = {
                    '--owner': OWNER,
                    '--repository': REPOSITORY,
                    '--branch': BRANCH,
                }

                run_task(ctx, 'clone-repo', LOG_CALLBACK,
                         flags_dict=clone_flags,
                         env={'GITHUB_TOKEN': GITHUB_TOKEN})

            ##
            # BUILD
            #
            build_flags = {
                '--branch': BRANCH,
                '--owner': OWNER,
                '--repository': REPOSITORY,
                '--site-prefix': SITE_PREFIX,
                '--base-url': BASEURL,
            }

            # Run the npm `federalist` task (if it is defined)
            run_task(ctx, 'run-federalist-script', LOG_CALLBACK, build_flags)

            # Run the appropriate build engine based on GENERATOR
            if GENERATOR == 'jekyll':
                build_flags['--config'] = CONFIG
                run_task(ctx, 'build-jekyll', LOG_CALLBACK, build_flags)
            elif GENERATOR == 'hugo':
                # extra: --hugo-version (not yet used)
                run_task(ctx, 'build-hugo', LOG_CALLBACK, build_flags)
            elif GENERATOR == 'static':
                # no build arguments are needed
                run_task(ctx, 'build-static', LOG_CALLBACK)
            else:
                raise ValueError(f'Invalid GENERATOR: {GENERATOR}')

            ##
            # PUBLISH
            #
            publish_flags = {
                '--base-url': BASEURL,
                '--site-prefix': SITE_PREFIX,
                '--bucket': BUCKET,
                '--cache-control': CACHE_CONTROL,
                '--aws-region': AWS_DEFAULT_REGION,
            }

            publish_env = {
                'AWS_ACCESS_KEY_ID': AWS_ACCESS_KEY_ID,
                'AWS_SECRET_ACCESS_KEY': AWS_SECRET_ACCESS_KEY,
            }

            run_task(ctx, 'publish', LOG_CALLBACK,
                     flags_dict=publish_flags,
                     env=publish_env)

            delta = datetime.now() - start_time
            delta_mins = int(delta.total_seconds() // 60)
            delta_leftover_secs = int(delta.total_seconds() % 60)
            LOGGER.info(
                f'Total build time: {delta_mins}m {delta_leftover_secs}s')

            # Finished!
            post_build_complete(STATUS_CALLBACK,
                                FEDERALIST_BUILDER_CALLBACK)

    except TimeoutException:
        LOGGER.info('Build has timed out')
        post_build_timeout(LOG_CALLBACK,
                           STATUS_CALLBACK,
                           FEDERALIST_BUILDER_CALLBACK)
    except Exception as err:  # pylint: disable=W0703

        # TODO: Need to make sure tokens, aws keys, etc
        # are not exposed here

        # TODO: might want a different approach with
        # how we pass those keys to the sub-steps. Like
        # maybe they should use env vars if present?

        # TODO: also seeing this, which is not helpful:
        #
        # Stderr: already printed
        #
        # Cloning into './tmp/site_repo'...
        # Exit code: 128
        # Stdout: already printed
        #
        # ^ all of that comes from pyinvoke itself
        #   not sure how to handle it.
        # https://github.com/pyinvoke/invoke/blob/8d65acf06b6d9fe48e7e48d1ccca325a232bc667/invoke/exceptions.py#L69

        LOGGER.info(f'Exception raised during build')

        err_string = replace_private_values(
            str(err),
            [GITHUB_TOKEN, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY]
        )

        LOGGER.debug(f'Posting build error:\n {err_string}')

        post_build_error(LOG_CALLBACK,
                         STATUS_CALLBACK,
                         FEDERALIST_BUILDER_CALLBACK,
                         err_string)

