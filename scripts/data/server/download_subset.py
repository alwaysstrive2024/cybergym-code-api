import argparse
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import docker

client = docker.from_env()

task_ids = [
    "arvo:47101",
    "arvo:3938",
    "arvo:24993",
    "arvo:1065",
    "arvo:10400",
    "arvo:368",
    "oss-fuzz:42535201",
    "oss-fuzz:42535468",
    "oss-fuzz:370689421",
    "oss-fuzz:385167047",
]


def pull_images(repo, tags, max_workers=1, max_attempts=3, retry_delay=30):
    def _pull(tag):
        image = f"{repo}:{tag}"
        try:
            client.images.get(image)
        except docker.errors.ImageNotFound:
            pass
        else:
            print(f"Already present {image}")
            return True

        for attempt in range(1, max_attempts + 1):
            print(f"Pulling {image} (attempt {attempt}/{max_attempts})...")
            try:
                # All official subset images are public.  Passing an explicit
                # empty config avoids a stale local credential helper that
                # otherwise prevents anonymous pulls before contacting Docker
                # Hub. Docker retains any layers it can cache between retries.
                client.images.pull(repo, tag=tag, auth_config={})
                client.images.get(image)
                print(f"Successfully pulled {image}")
                return True
            except docker.errors.DockerException as e:
                print(f"Failed to pull {image} (attempt {attempt}/{max_attempts}): {e}")
                if attempt < max_attempts:
                    delay = retry_delay * attempt
                    print(f"Retrying {image} in {delay}s...")
                    time.sleep(delay)

        print(f"Exhausted retries for {image}")
        return False

    if max_workers == 1:
        return [f"{repo}:{tag}" for tag in tags if not _pull(tag)]

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_pull, tag): tag for tag in tags}
        failures = []
        for future in as_completed(futures):
            try:
                if not future.result():
                    failures.append(f"{repo}:{futures[future]}")
            except Exception as e:
                tag = futures[future]
                print(f"Unexpected error pulling {repo}:{tag}: {e}")
                failures.append(f"{repo}:{tag}")
        return failures


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download the server data.")
    parser.add_argument(
        "--max-workers", "-w", type=int, default=1, help="Maximum number of concurrent workers (default: 1)"
    )
    parser.add_argument(
        "--max-attempts", type=int, default=3, help="Maximum pull attempts per missing image (default: 3)"
    )
    parser.add_argument(
        "--retry-delay", type=int, default=30, help="Initial retry delay in seconds (default: 30)"
    )
    args = parser.parse_args()

    failures = pull_images(
        "cybergym/oss-fuzz-base-runner", ["latest"], 1, args.max_attempts, args.retry_delay
    )

    tags_arvo = []
    tags_ossfuzz = []
    for task_id in task_ids:
        if task_id.split(":")[0] == "arvo":
            arvo_id = task_id.split(":")[-1]
            tags_arvo.append(f"{arvo_id}-vul")
            tags_arvo.append(f"{arvo_id}-fix")
        if task_id.split(":")[0] == "oss-fuzz":
            ossfuzz_id = task_id.split(":")[-1]
            tags_ossfuzz.append(f"{ossfuzz_id}-vul")
            tags_ossfuzz.append(f"{ossfuzz_id}-fix")
    if tags_arvo:
        failures.extend(pull_images("n132/arvo", tags_arvo, args.max_workers, args.max_attempts, args.retry_delay))
    if tags_ossfuzz:
        failures.extend(
            pull_images("cybergym/oss-fuzz", tags_ossfuzz, args.max_workers, args.max_attempts, args.retry_delay)
        )

    if failures:
        print("Images still missing after retries:")
        for image in failures:
            print(f"  {image}")
        raise SystemExit(1)
