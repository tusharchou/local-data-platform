# Exception

Exception handling is crucial for building robust, reliable, and user-friendly Python libraries. Good exception handling communicates issues clearly to the library user, helps with debugging, and prevents silent failures.

Here are the best practices for exception handling in a Python library:

1.  **Don't Silence Exceptions (The Golden Rule):**

      * **Avoid `except: pass` or `except Exception: pass`.** This is the most common and dangerous anti-pattern. It hides bugs, makes debugging impossible, and leads to unexpected behavior in user applications.
      * **Instead, at a minimum, log the exception and re-raise it, or transform it into a more specific, higher-level exception.**

2.  **Be Specific with `except` Clauses:**

      * Catch only the specific exceptions you expect and know how to handle.
      * **Bad:**
        ```python
        try:
            # network operation
        except Exception as e:
            print(f"An error occurred: {e}")
            # This catches everything, including KeyboardInterrupt, SystemExit, etc.
        ```
      * **Good:**
        ```python
        import requests
        try:
            response = requests.get("http://example.com/api")
            response.raise_for_status()
        except requests.exceptions.Timeout:
            raise MyLibraryTimeoutError("API request timed out.") from None
        except requests.exceptions.ConnectionError:
            raise MyLibraryNetworkError("Could not connect to the API server.") from None
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                raise MyLibraryResourceNotFoundError("Requested resource not found.") from None
            else:
                # Re-raise generic HTTP errors or wrap in a generic library error
                raise MyLibraryAPIError(f"API returned an error: {e.response.status_code}") from e
        except Exception as e: # Catching a broader Exception at the very end as a last resort
            # Log the unexpected exception details for debugging
            import logging
            logging.exception("An unexpected error occurred in API call")
            raise MyLibraryUnknownError("An unexpected error occurred.") from e
        ```

3.  **Raise Custom Exceptions:**

      * **Why:** This is paramount for libraries. Custom exceptions provide clear, semantic meaning to errors originating from your library. Users can then specifically catch *your* library's errors without accidentally catching unrelated issues from other parts of their application or other libraries.
      * **How:** Create a base exception for your library, and then derive more specific exceptions from it.
        ```python
        # In your library's exceptions.py (or similar)
        class MyLibraryError(Exception):
            """Base exception for MyLibrary."""
            pass

        class MyLibraryConnectionError(MyLibraryError):
            """Raised when a connection to a service fails."""
            pass

        class MyLibraryConfigError(MyLibraryError):
            """Raised when the library configuration is invalid."""
            pass

        class MyLibraryResourceNotFoundError(MyLibraryError):
            """Raised when a specific resource cannot be found."""
            pass
        ```
      * **Usage:**
        ```python
        if not os.path.exists(config_path):
            raise MyLibraryConfigError(f"Configuration file not found at: {config_path}")
        ```

4.  **Provide Clear and Informative Error Messages:**

      * When raising or re-raising an exception, the message should explain *what went wrong*, *why it went wrong*, and ideally, *how the user might fix it* (if applicable).
      * Include relevant context: input values, file paths, IDs, error codes from external services.
      * **Bad:** `raise MyLibraryError("Something went wrong.")`
      * **Good:** `raise MyLibraryConnectionError(f"Failed to connect to {url}. Please check your network connection.")`

5.  **Use Exception Chaining (`raise ... from ...`):**

      * When you catch a lower-level exception and re-raise a new, higher-level (custom) exception, use `raise NewException(...) from OriginalException`.
      * This preserves the original exception's traceback, providing a full "cause" chain, which is invaluable for debugging.
      * Use `from None` if you *don't* want the original exception to be implicitly chained (e.g., if it's an internal detail you've fully handled and transformed).

    <!-- end list -->

    ```python
    import json
    class MyLibraryParseError(MyLibraryError): pass

    try:
        data = json.loads(invalid_json_string)
    except json.JSONDecodeError as e:
        # Chaining preserves the original JSONDecodeError traceback
        raise MyLibraryParseError("Failed to parse JSON data.") from e
    ```

6.  **Use `finally` for Cleanup:**

      * The `finally` block *always* executes, regardless of whether an exception occurred in the `try` block or not.
      * This is ideal for releasing resources like file handles, network connections, database cursors, or locks.

    <!-- end list -->

    ```python
    file_handle = None
    try:
        file_handle = open("my_file.txt", "w")
        file_handle.write("Hello")
    except IOError as e:
        raise MyLibraryIOError("Could not write to file.") from e
    finally:
        if file_handle:
            file_handle.close() # Guarantees the file is closed
    ```

7.  **Prefer `with` statements for Resource Management:**

      * For resources that support the context manager protocol (like files, locks, database connections), the `with` statement is generally preferred over `try-finally` for cleanup. It automatically handles `__enter__` and `__exit__` methods, ensuring resources are properly acquired and released even if exceptions occur.

    <!-- end list -->

    ```python
    try:
        with open("my_file.txt", "w") as f:
            f.write("Hello")
        # File is automatically closed here, even if f.write() failed
    except IOError as e:
        raise MyLibraryIOError("Could not write to file.") from e
    ```

8.  **Log Exceptions, Don't Print:**

      * Use Python's `logging` module instead of `print()` for debugging and operational messages.
      * Logging allows users of your library to configure how and where messages are stored (console, file, syslog, etc.) and at what level of detail (DEBUG, INFO, WARNING, ERROR, CRITICAL).
      * `logging.exception()` is particularly useful as it automatically includes traceback information.

    <!-- end list -->

    ```python
    import logging
    logger = logging.getLogger(__name__) # Or get a common library logger

    try:
        # some risky operation
    except SomeError as e:
        logger.error(f"Failed operation due to: {e}") # Basic error message
        logger.exception("Detailed traceback for debugging this failure:") # Full traceback
        raise # Re-raise after logging
    ```

9.  **Design Your API Around Exceptions:**

      * Document the exceptions your public functions and methods might raise. This forms part of your library's contract with its users. Users need to know what errors to anticipate and handle.
      * Avoid leaking internal implementation details via low-level exceptions. Wrap them in your library's custom exceptions.

10. **Avoid Catching `BaseException`:**

      * `BaseException` is the root of *all* exceptions, including `SystemExit` (raised by `sys.exit()`) and `KeyboardInterrupt` (Ctrl+C). Catching `BaseException` will prevent your program from exiting cleanly or responding to interrupts.
      * Generally, you should only catch `Exception` (which `SystemExit` and `KeyboardInterrupt` do *not* inherit from).

By adhering to these best practices, you can create Python libraries that are robust, easy to debug, and provide a clear, predictable error handling experience for their users.