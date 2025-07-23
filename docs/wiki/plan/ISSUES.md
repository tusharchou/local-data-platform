# How to Contribute

We're thrilled that you're interested in contributing to the Local Data Platform! Your help is essential for keeping it great.

This section provides guidelines for contributing to the project. Please take a moment to review this document in order to make the contribution process easy and effective for everyone involved.
Following these guidelines helps to communicate that you respect the time of the developers managing and developing this open-source project. In return, they should reciprocate that respect in addressing your issue, assessing changes, and helping you finalize your pull requests.

## How to Get Started
## Finding Things to Work On

If you're new to the project, the best place to start is the `how_to_setup.md` guide located in the root of the repository. This will walk you through cloning the project and setting up your local development environment.
The best way to start contributing is to look for an existing issue to solve.

Once you're set up, you can explore the other pages in this section to learn how to report issues or request features.
*   **Understand the Project Layout**: Before diving into the code, get familiar with the **Directory Structure**.
*   **Find an Issue**: Check out the list of **Open Issues** to find tasks that are ready for contributors. This is the best place to start.
*   **See What's Been Done**: To get a sense of the project's momentum and past contributions, you can review the list of **All Closed Items**.

If you have a new idea, please open a Feature Request to discuss it first.

## Submitting Pull Requests

We follow a standard "fork and pull" model for contributions. To submit a change, please follow these steps:
Once you've chosen an issue and are ready to submit your code, please follow these steps:

1.  **Create a Fork**: Fork the repository to your own GitHub account.
2.  **Create a Branch**: Create a new branch from `main` in your fork for your changes. Please use a descriptive branch name (e.g., `feat/add-new-ingestion-source` or `fix/docs-build-error`).
3.  **Make Your Changes**: Make your changes, ensuring you follow the project's coding style.
4.  **Run Quality Checks**: Before committing, run all the local quality checks to ensure your changes don't introduce any issues.
    ```sh
    make all
    make all # This runs both linting and tests
    ```
5.  **Commit Your Changes**: Commit your changes with a clear and descriptive commit message. We follow the Conventional Commits specification.
6.  **Push to Your Fork**: Push your branch to your fork on GitHub.
7.  **Open a Pull Request**: From your fork on GitHub, open a pull request to the `main` branch of the `tusharchou/local-data-platform` repository.

Your PR will be reviewed by the maintainers, and once approved, it will be merged. Thank you for your contribution!
