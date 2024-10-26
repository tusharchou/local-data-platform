import subprocess

def run_dbt_command(command: str) -> None:
    """Run a dbt command."""
    result = subprocess.run(command.split(), capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error running dbt: {result.stderr}")
        raise RuntimeError(result.stderr)
    else:
        print(result.stdout)

def transform_silver_to_gold():
    """Run the dbt transformation from Silver to Gold."""
    print("Running dbt to transform Silver to Gold...")
    run_dbt_command("dbt run --models silver_to_gold")

if __name__ == "__main__":
    transform_silver_to_gold()
