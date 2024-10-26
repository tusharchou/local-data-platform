import subprocess

def run_script(script_name: str) -> None:
    """Run a Python script."""
    print(f"Running {script_name}...")
    result = subprocess.run(["python", script_name], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error in {script_name}: {result.stderr}")
        raise RuntimeError(result.stderr)
    print(result.stdout)

if __name__ == "__main__":
    # Step 1: Bronze Layer
    run_script("bronze_layer.py")

    # Step 2: Silver Layer
    run_script("silver_layer.py")

    # Step 3: Gold Layer
    run_script("gold_layer.py")
