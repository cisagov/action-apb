"""GitHub Action to rebuild respositories that haven't been built in a while."""

# Standard Python Libraries
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import sys
from typing import Generator, Optional

# Third-Party Libraries
from actions_toolkit import core
from babel.dates import format_timedelta
from dateutil.relativedelta import relativedelta
from github import Github, GithubException, Repository, Workflow
import pytimeparse


def get_repo_list(
    g: Github, repo_query: str
) -> Generator[Repository.Repository, None, None]:
    """Generate a list of repositories based on the query."""
    logging.info("Querying for repositories: %s", repo_query)
    matching_repos = g.search_repositories(query=repo_query)
    for repo in matching_repos:
        yield repo


def get_workflow(
    repo: Repository.Repository, workflow_id: str
) -> Optional[Workflow.Workflow]:
    """Retrieve the desired workflow from the target repository."""
    logging.debug(
        "Retrieving workflow '%s' for repository %s", workflow_id, repo.full_name
    )

    try:
        workflow = repo.get_workflow(workflow_id)
    except GithubException as err:
        if err.status == 404:
            logging.info(
                "No workflow with id '%s' was found in %s", workflow_id, repo.full_name
            )
            return None

        logging.warning(
            "Error retrieving workflow '%s' in %s: %s", workflow_id, repo.full_name, err
        )
        return None

    return workflow


def get_last_run(workflow: Workflow.Workflow, target_branch: str) -> Optional[datetime]:
    """Get the last run time for the given workflow."""
    logging.debug(
        "Requesting runs for workflow %s on branch %s", workflow.name, target_branch
    )

    workflow_runs = workflow.get_runs(branch=target_branch)

    if workflow_runs.totalCount == 0:
        return None

    return workflow_runs[0].created_at.replace(tzinfo=None)


def main() -> None:
    """Parse evironment and perform requested actions."""
    # Set up logging
    logging.basicConfig(
        format="%(levelname)s %(message)s", level=logging.INFO, stream=sys.stdout
    )

    # Get inputs from the environment
    access_token: Optional[str] = os.environ.get("INPUT_ACCESS_TOKEN")
    build_age: Optional[str] = os.environ.get("INPUT_BUILD_AGE")
    event_type: Optional[str] = os.environ.get("INPUT_EVENT_TYPE")
    github_workspace_dir: Optional[str] = os.environ.get("GITHUB_WORKSPACE")
    include_non_public: bool = core.get_boolean_input("include_non_public_repos")
    mask_non_public: bool = core.get_boolean_input("mask_non_public_repos")
    max_rebuilds: int = int(os.environ.get("INPUT_MAX_REBUILDS", 10))
    repo_query: Optional[str] = os.environ.get("INPUT_REPO_QUERY")
    workflow_id: Optional[str] = os.environ.get("INPUT_WORKFLOW_ID")
    write_filename: Optional[str] = os.environ.get("INPUT_WRITE_FILENAME", "apb.json")

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

    logging.info("Rebuilding repositories that haven't run since %s", past_date)

    # Create a Github access object
    g = Github(access_token)

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
    repos_sent_events = []
    for repo in repos:
        repo_status: dict = dict()
        all_repo_status["repositories"][repo.full_name] = repo_status
        # Extra controls if the repo is non-public
        if repo.private:
            if not include_non_public:
                del all_repo_status["repositories"][repo.full_name]
                continue
            # Ensure that non-public repo names do not show up in the logs
            if mask_non_public:
                core.set_secret(repo.full_name)
                del all_repo_status["repositories"][repo.full_name]
        core.start_group(repo.full_name)
        target_workflow = get_workflow(repo, workflow_id)
        if target_workflow is None:
            # Repo does not have the workflow configured
            logging.info("%s does not have workflow %s", repo.full_name, workflow_id)
            repo_status["workflow"] = None
            core.end_group()
            continue
        last_run = get_last_run(target_workflow, repo.default_branch)
        if last_run is None:
            # Repo does not have any workflow runs that count
            logging.info(
                "%s does not have workflow runs for %s on default branch %s",
                repo.full_name,
                workflow_id,
                repo.default_branch,
            )
            repo_status["workflow"] = None
            core.end_group()
            continue
        # repo has the workflow we're looking for
        repo_status["workflow"] = workflow_id
        delta = now - last_run
        repo_status["run_age_seconds"] = int(delta.total_seconds())
        repo_status["run_age"] = format_timedelta(delta)
        repo_status["event_sent"] = False
        if last_run < past_date:
            logging.info(
                "%s needs a rebuild: %s", repo.full_name, format_timedelta(delta)
            )
            if max_rebuilds == 0 or rebuilds_triggered < max_rebuilds:
                rebuilds_triggered += 1
                repo.create_repository_dispatch(event_type)
                repo_status["event_sent"] = True
                logging.info(
                    "Sent %s event #%d to %s.",
                    event_type,
                    rebuilds_triggered,
                    repo.full_name,
                )
                repos_sent_events.append(repo.full_name)
                if rebuilds_triggered == max_rebuilds:
                    logging.warning("Max rebuild events sent.")
        else:
            logging.info("%s is OK: %s", repo.full_name, format_timedelta(delta))
        core.end_group()

    core.notice(
        "\n".join(["Repositories sent events:", *repos_sent_events]),
        title=f"Sent {rebuilds_triggered} '{event_type}' events",
    )

    # Write json state to an output file
    status_file: Path = Path(github_workspace_dir) / Path(write_filename)
    logging.info("Writing status file to %s", status_file)
    with status_file.open("w") as f:
        json.dump(all_repo_status, f, indent=4, sort_keys=True)
    logging.info("Completed.")
