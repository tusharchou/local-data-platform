import subprocess

def run_dbt_command(command: str) -> None:
    """Run a dbt command."""
    result = subprocess.run(command.split(), capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error running dbt: {result.stderr}")
        raise RuntimeError(result.stderr)
    else:
        print(result.stdout)

def transform_bronze_to_silver():
    """Run the dbt transformation from Bronze to Silver."""
    print("Running dbt to transform Bronze to Silver...")
    run_dbt_command("dbt run --models bronze_to_silver")

if __name__ == "__main__":
    transform_bronze_to_silver()
