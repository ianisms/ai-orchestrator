from pathlib import Path
import subprocess

def main():
    subprocess.run(["python", "/ai/server/scripts/kill_zombiez.py"], check=False)

    target_service = ""

    subprocess.run(
        ["docker", "compose", "down", "--remove-orphans"],
        cwd="/ai/server/docker",
        check=True,
    )

    up_cmd = ["docker", "compose", "up", "-d", "--build", "--force-recreate"]
    logs_cmd = ["docker", "compose", "logs", "-f"]

    if target_service:
        up_cmd.append(target_service)
        logs_cmd.append(target_service)

    subprocess.run(up_cmd, cwd="/ai/server/docker", check=True)
    subprocess.run(logs_cmd, cwd="/ai/server/docker", check=False)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        exit(0)
