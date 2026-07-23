"""File utility functions for MHES."""

import os


def allowed_file(filename: str, allowed_extensions: set[str]) -> bool:
    """Check if a filename has an allowed extension.

    Args:
        filename: Name of the file to check.
        allowed_extensions: Set of allowed file extensions.

    Returns:
        True if the file extension is allowed.
    """
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed_extensions


def ensure_directory(path: str) -> None:
    """Ensure a directory exists, creating it if necessary.

    Args:
        path: Directory path to ensure exists.
    """
    os.makedirs(path, exist_ok=True)


def get_file_size_mb(file_path: str) -> float:
    """Get file size in megabytes.

    Args:
        file_path: Path to the file.

    Returns:
        File size in MB.
    """
    return os.path.getsize(file_path) / (1024 * 1024)
