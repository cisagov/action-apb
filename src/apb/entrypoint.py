"""Tool to rebuild repositories that haven't been built in a while.

This tool allows you to use environment variables to provide necessary information. Any
options provided on the command-line will be used instead of any environment variables
that have been set.

Usage:
  apb [--log-level <level>] [--github-token <token>] [--repo-query <query>] [--workflow-name <workflow>] [--build-age <age>] [--event-type >event>] [--max-rebuilds <limit>] [--output-dir <dir>] [--output-file <file>]

Options:
  --github-token <token>      The GitHub Personal Access Token to use for authentication.
  --repo-query <query>        The query used to find GitHub repositories to check.
  --workflow-name <workflow>  The name of the workflow that should be checked by the
                              tool.
  --build-age <age>           The maximum age of the last workflow run before a repository
                              dispatch is created.
  --event-type >event>        The type of event to send when a repository dispatch is
                              created.
  --max-rebuilds <limit>      The maximum number of rebuilds dispatches to create in a
                              single run of the tool.
  --output-dir <dir>          The directory that should house the created state file.
  --output-file <file>        The name to use when creating the state file.
  --log-level <level>         The log level to use if specified. Valid values are
                              "debug", "info", "warning", "error", and "critical".
                              [default: info]
"""

# Standard Python Libraries
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import sys
from typing import Dict, Generator, Optional

# Third-Party Libraries
from babel.dates import format_timedelta
from dateutil.parser import isoparse
from dateutil.relativedelta import relativedelta
import docopt
from github import Github, Repository
import pytimeparse
import requests

from ._version import __version__


def get_repo_list(
    g: Github, repo_query: str
) -> Generator[Repository.Repository, None, None]:
    """Generate a list of repositories based on the query."""
    print(f"Querying for repositories: {repo_query}")
    matching_repos = g.search_repositories(query=repo_query)
    yield from matching_repos


def get_last_run(
    session: requests.Session, repo: Repository.Repository, workflow_id: str
) -> Optional[datetime]:
    """Get the last run time for a workflow in a respository."""
    logging.debug(f"Requesting workflow runs for repository {repo.name}")
    response = session.get(
        f"https://api.github.com/repos/{repo.full_name}/actions/workflows/{workflow_id}/runs"
    )
    if response.status_code != 200:
        logging.debug(
            f"No previous runs for {workflow_id} in {repo.full_name}, {response.status_code}"
        )
        return None
    workflow_runs = response.json()["workflow_runs"]
    if len(workflow_runs) == 0:
        return None
    else:
        last_run_date = workflow_runs[0]["created_at"]
        return isoparse(last_run_date).replace(tzinfo=None)


def main() -> None:
    """Parse evironment and perform requested actions."""
    args: Dict[str, str] = docopt.docopt(__doc__, version=__version__)
    # Get the list of logging levels supported by the logging library. Filter out
    # values that are deprecated or not actual logging levels.
    log_levels = [
        level.lower()
        for level in list(logging._nameToLevel.keys() - ["NOTSET", "WARN"])
    ]
    if args["--log-level"].lower() not in log_levels:
        print(
            "Possible values for --log-level are "
            + ", ".join(log_levels[:-1])
            + ", and "
            + log_levels[-1],
            file=sys.stderr,
        )
        sys.exit(-1)
    # Set up logging
    logging.basicConfig(
        format="%(levelname)s %(message)s", level=args["--log-level"].upper()
    )

    # Get inputs from the environment
    access_token: Optional[str] = (
        args["--github-token"]
        if args["--github-token"] is not None
        else os.environ.get("INPUT_ACCESS_TOKEN")
    )
    build_age: Optional[str] = (
        args["--build-age"]
        if args["--build-age"] is not None
        else os.environ.get("INPUT_BUILD_AGE")
    )
    event_type: Optional[str] = (
        args["--event-type"]
        if args["--event-type"] is not None
        else os.environ.get("INPUT_EVENT_TYPE")
    )
    github_workspace_dir: Optional[str] = (
        args["--output-dir"]
        if args["--output-dir"] is not None
        else os.environ.get("GITHUB_WORKSPACE")
    )
    max_rebuilds: int = int(
        args["--max-rebuilds"]
        if args["--max-rebuilds"] is not None
        else os.environ.get("INPUT_MAX_REBUILDS", "10")
    )
    repo_query: Optional[str] = (
        args["--repo-query"]
        if args["--repo-query"] is not None
        else os.environ.get("INPUT_REPO_QUERY")
    )
    workflow_id: Optional[str] = (
        args["--workflow-name"]
        if args["--workflow-name"] is not None
        else os.environ.get("INPUT_WORKFLOW_ID")
    )
    write_filename: Optional[str] = (
        args["--output-file"]
        if args["--output-file"] is not None
        else os.environ.get("INPUT_WRITE_FILENAME", "apb.json")
    )

    # sanity checks
    if access_token is None:
        logging.fatal(
            "Access token environment variable must be set. (INPUT_ACCESS_TOKEN)"
        )
        sys.exit(-1)

    if build_age is None:
        logging.fatal("Build age environment variable must be set. (INPUT_BUILD_AGE)")
        sys.exit(-1)

    if event_type is None:
        logging.fatal("Event type environment variable must be set. (INPUT_EVENT_TYPE)")
        sys.exit(-1)

    if github_workspace_dir is None:
        logging.fatal(
            "GitHub workspace environment variable must be set. (GITHUB_WORKSPACE)"
        )
        sys.exit(-1)

    if repo_query is None:
        logging.fatal(
            "Reository query environment variable must be set. (INPUT_REPO_QUERY)"
        )
        sys.exit(-1)

    if workflow_id is None:
        logging.fatal(
            "Workflow ID environment variable must be set. (INPUT_WORKFLOW_ID)"
        )
        sys.exit(-1)

    if write_filename is None:
        logging.fatal(
            "Output filename environment variable must be set. (INPUT_WRITE_FILENAME)"
        )
        sys.exit(-1)

    # setup time calculations
    now: datetime = datetime.utcnow()
    build_age_seconds: int = pytimeparse.parse(build_age)
    time_delta: relativedelta = relativedelta(seconds=build_age_seconds)
    past_date: datetime = now - time_delta

    logging.info(f"Rebuilding repositories that haven't run since {past_date}")

    # Create a Github access object
    g = Github(access_token)

    # Set up a session to do things the Github library has not yet implemented.
    session: requests.Session = requests.Session()
    session.auth = ("", access_token)

    repos = get_repo_list(g, repo_query)

    rebuilds_triggered: int = 0
    # Gather status for output at end of run
    all_repo_status: dict = {
        "build_age_seconds": build_age_seconds,
        "build_age": build_age,
        "ran_at": now.isoformat(),
        "repositories": dict(),
        "repository_query": repo_query,
    }
    for repo in repos:
        repo_status: dict = dict()
        all_repo_status["repositories"][repo.full_name] = repo_status
        last_run = get_last_run(session, repo, workflow_id)
        if last_run is None:
            # repo does not have the workflow configured
            logging.info(f"{repo.full_name} does not have workflow {workflow_id}")
            repo_status["workflow"] = None
            continue
        # repo has the workflow we're looking for
        repo_status["workflow"] = workflow_id
        delta = now - last_run
        repo_status["run_age_seconds"] = int(delta.total_seconds())
        repo_status["run_age"] = format_timedelta(delta)
        repo_status["event_sent"] = False
        if last_run < past_date:
            logging.info(f"{repo.full_name} needs a rebuild: {format_timedelta(delta)}")
            if max_rebuilds == 0 or rebuilds_triggered < max_rebuilds:
                rebuilds_triggered += 1
                repo.create_repository_dispatch(event_type)
                repo_status["event_sent"] = True
                logging.info(
                    f"Sent {event_type} event #{rebuilds_triggered} to {repo.full_name}."
                )
                if rebuilds_triggered == max_rebuilds:
                    logging.warning("Max rebuild events sent.")
        else:
            logging.info(f"{repo.full_name} is OK: {format_timedelta(delta)}")

    # Write json state to an output file
    status_file: Path = Path(github_workspace_dir) / Path(write_filename)
    logging.info(f"Writing status file to {status_file}")
    with status_file.open("w") as f:
        json.dump(all_repo_status, f, indent=4, sort_keys=True)
    logging.info("Completed.")
