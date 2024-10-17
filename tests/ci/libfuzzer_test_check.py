#!/usr/bin/env python3

import argparse
import logging
import os
import sys
import zipfile
from pathlib import Path
from typing import List

from botocore.exceptions import ClientError

from build_download_helper import download_fuzzers
from clickhouse_helper import CiLogsCredentials
from docker_images_helper import DockerImage, get_docker_image, pull_image
from env_helper import REPO_COPY, REPORT_PATH, S3_BUILDS_BUCKET, TEMP_PATH
from pr_info import PRInfo
from s3_helper import S3Helper
from stopwatch import Stopwatch
from tee_popen import TeePopen

NO_CHANGES_MSG = "Nothing to run"
s3 = S3Helper()


def zipdir(path, ziph):
    # ziph is zipfile handle
    for root, dirs, files in os.walk(path):
        for file in files:
            ziph.write(
                os.path.join(root, file), 
                os.path.relpath(os.path.join(root, file), os.path.join(path, '..')),
            )


def get_additional_envs(check_name, run_by_hash_num, run_by_hash_total):
    result = []
    if "DatabaseReplicated" in check_name:
        result.append("USE_DATABASE_REPLICATED=1")
    if "DatabaseOrdinary" in check_name:
        result.append("USE_DATABASE_ORDINARY=1")
    if "wide parts enabled" in check_name:
        result.append("USE_POLYMORPHIC_PARTS=1")
    if "ParallelReplicas" in check_name:
        result.append("USE_PARALLEL_REPLICAS=1")
    if "s3 storage" in check_name:
        result.append("USE_S3_STORAGE_FOR_MERGE_TREE=1")
        result.append("RANDOMIZE_OBJECT_KEY_TYPE=1")
    if "analyzer" in check_name:
        result.append("USE_OLD_ANALYZER=1")

    if run_by_hash_total != 0:
        result.append(f"RUN_BY_HASH_NUM={run_by_hash_num}")
        result.append(f"RUN_BY_HASH_TOTAL={run_by_hash_total}")

    return result


def get_run_command(
    fuzzers_path: Path,
    repo_path: Path,
    result_path: Path,
    additional_envs: List[str],
    ci_logs_args: str,
    image: DockerImage,
) -> str:
    additional_options = ["--hung-check"]
    additional_options.append("--print-time")

    additional_options_str = (
        '-e ADDITIONAL_OPTIONS="' + " ".join(additional_options) + '"'
    )

    envs = [
        # a static link, don't use S3_URL or S3_DOWNLOAD
        '-e S3_URL="https://s3.amazonaws.com"',
    ]

    envs += [f"-e {e}" for e in additional_envs]

    env_str = " ".join(envs)

    return (
        f"docker run "
        f"{ci_logs_args} "
        f"--workdir=/fuzzers "
        f"--volume={fuzzers_path}:/fuzzers "
        f"--volume={repo_path}/tests:/usr/share/clickhouse-test "
        f"--volume={result_path}:/test_output "
        "--security-opt seccomp=unconfined "  # required to issue io_uring sys-calls
        f"--cap-add=SYS_PTRACE {env_str} {additional_options_str} {image} "
        "python3 /usr/share/clickhouse-test/fuzz/runner.py"
    )


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("check_name")
    return parser.parse_args()


def download_corpus(corpus_path: str, fuzzer_name: str):
    logging.info("Download corpus for %s ...", fuzzer_name)

    units = []

    try:
        units = s3.download_files(
            bucket=S3_BUILDS_BUCKET,
            s3_path=f"fuzzer/corpus/{fuzzer_name}/",
            file_suffix="",
            local_directory=corpus_path,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            logging.debug("No active corpus exists for %s", fuzzer_name)
        else:
            raise

    logging.info("...downloaded %d units", len(units))


def upload_corpus(result_path: str):
    with zipfile.ZipFile(
        f"{result_path}/corpus.zip", "w", zipfile.ZIP_DEFLATED
    ) as zipf:
        zipdir(f"{result_path}/corpus/", zipf)
    s3.upload_file(
        bucket=S3_BUILDS_BUCKET,
        file_path=f"{result_path}/corpus.zip",
        s3_path="fuzzer/corpus.zip",
    )
    # for file in os.listdir(f"{result_path}/corpus/"):
    #     s3.upload_build_directory_to_s3(
    #         Path(f"{result_path}/corpus/{file}"), f"fuzzer/corpus/{file}", False
    #     )


def main():
    logging.basicConfig(level=logging.INFO)

    stopwatch = Stopwatch()

    temp_path = Path(TEMP_PATH)
    reports_path = Path(REPORT_PATH)
    temp_path.mkdir(parents=True, exist_ok=True)
    repo_path = Path(REPO_COPY)

    args = parse_args()
    check_name = args.check_name

    pr_info = PRInfo()

    temp_path.mkdir(parents=True, exist_ok=True)

    if "RUN_BY_HASH_NUM" in os.environ:
        run_by_hash_num = int(os.getenv("RUN_BY_HASH_NUM", "0"))
        run_by_hash_total = int(os.getenv("RUN_BY_HASH_TOTAL", "0"))
    else:
        run_by_hash_num = 0
        run_by_hash_total = 0

    docker_image = pull_image(get_docker_image("clickhouse/libfuzzer"))

    fuzzers_path = temp_path / "fuzzers"
    fuzzers_path.mkdir(parents=True, exist_ok=True)

    download_fuzzers(check_name, reports_path, fuzzers_path)

    for file in os.listdir(fuzzers_path):
        if file.endswith("_fuzzer"):
            os.chmod(fuzzers_path / file, 0o777)
            download_corpus(f"{fuzzers_path}/{file}.corpus", file)
        elif file.endswith("_seed_corpus.zip"):
            corpus_path = fuzzers_path / (file.removesuffix("_seed_corpus.zip") + ".in")
            with zipfile.ZipFile(fuzzers_path / file, "r") as zfd:
                zfd.extractall(corpus_path)

    result_path = temp_path / "result_path"
    result_path.mkdir(parents=True, exist_ok=True)

    run_log_path = result_path / "run.log"

    additional_envs = get_additional_envs(
        check_name, run_by_hash_num, run_by_hash_total
    )

    # additional_envs.append("CI=1")

    ci_logs_credentials = CiLogsCredentials(Path(temp_path) / "export-logs-config.sh")
    ci_logs_args = ci_logs_credentials.get_docker_arguments(
        pr_info, stopwatch.start_time_str, check_name
    )

    run_command = get_run_command(
        fuzzers_path,
        repo_path,
        result_path,
        additional_envs,
        ci_logs_args,
        docker_image,
    )
    logging.info("Going to run libFuzzer tests: %s", run_command)

    with TeePopen(run_command, run_log_path) as process:
        retcode = process.wait()
        if retcode == 0:
            logging.info("Run successfully")
            upload_corpus(result_path)
        else:
            logging.info("Run failed")

    sys.exit(0)


if __name__ == "__main__":
    main()
