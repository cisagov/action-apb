"""GitHub Action to rebuild respositories that haven't been built in a while."""

# Standard Python Libraries
from collections.abc import Generator
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import sys

# Third-Party Libraries
from babel.dates import format_timedelta
from dateutil.parser import isoparse
from dateutil.relativedelta import relativedelta
from github import Github, Repository
import pytimeparse
import requests


def clear_ca_variables_in_gha() -> None:
    """Unset CA variables if running in GitHub Actions."""
    logging.info("Clearing CA variables...")
    # Only remove variables we care about
    for ca_variable in ["REQUESTS_CA_BUNDLE"]:
        if ca_variable in os.environ:
            del os.environ[ca_variable]
            logging.info("Removed %s from environment.", ca_variable)


def get_repo_list(
    g: Github, repo_query: str
) -> Generator[Repository.Repository, None, None]:
    """Generate a list of repositories based on the query."""
    print(f"Querying for repositories: {repo_query}")
    matching_repos = g.search_repositories(query=repo_query)
    yield from matching_repos


def get_last_run_on_default_branch(
    session: requests.Session, repo: Repository.Repository, workflow_id: str
) -> datetime | None:
    """Get last run time for a workflow on default branch in a respository."""
    logging.debug(f"Requesting repository information for repository {repo.name}")
    response = session.get(f"https://api.github.com/repos/{repo.full_name}")
    if response.status_code != 200:
        logging.debug(f"Invalid repository {repo.full_name}, {response.status_code}")
        return None
    default_branch = response.json()["default_branch"]

    logging.debug(
        f"Requesting workflow runs on {default_branch} for repository {repo.name}"
    )
    response = session.get(
        f"https://api.github.com/repos/{repo.full_name}/actions/workflows/{workflow_id}/runs"
    )
    if response.status_code != 200:
        logging.debug(
            f"No previous runs for {workflow_id} in {repo.full_name}, {response.status_code}"
        )
        return None

    # Find the date of the most recent workflow run against the default branch
    last_run_date = None
    for run in response.json()["workflow_runs"]:
        if run["head_branch"] == default_branch:
            last_run_date = isoparse(run["created_at"]).replace(tzinfo=None)
            break

    return last_run_date


def main() -> None:
    """Parse evironment and perform requested actions."""
    # Set up logging
    logging.basicConfig(format="%(levelname)s %(message)s", level="INFO")

    # Get inputs from the environment
    access_token: str | None = os.environ.get("INPUT_ACCESS_TOKEN")
    build_age: str | None = os.environ.get("INPUT_BUILD_AGE")
    event_type: str | None = os.environ.get("INPUT_EVENT_TYPE")
    github_workspace_dir: str | None = os.environ.get("GITHUB_WORKSPACE")
    max_rebuilds: int = int(os.environ.get("INPUT_MAX_REBUILDS", 10))
    repo_query: str | None = os.environ.get("INPUT_REPO_QUERY")
    workflow_id: str | None = os.environ.get("INPUT_WORKFLOW_ID")
    write_filename: str | None = os.environ.get("INPUT_WRITE_FILENAME", "apb.json")

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

    # If the GITHUB_ACTIONS environment variable is set to true it should indicate
    # that we are running in GitHub Actions. Please see the following documentation
    # for more information:
    # https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/store-information-in-variables#default-environment-variables
    if os.environ.get("GITHUB_ACTIONS", "false") == "true":
        clear_ca_variables_in_gha()

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
        "repositories": {},
        "repository_query": repo_query,
    }
    for repo in repos:
        repo_status: dict = {}
        all_repo_status["repositories"][repo.full_name] = repo_status
        last_run = get_last_run_on_default_branch(session, repo, workflow_id)
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
