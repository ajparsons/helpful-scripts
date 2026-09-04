#!/usr/bin/env -S uv run --script
"""
Simple version bumping script for projects using dynamic versioning.

Usage:
    python scripts/bump_version.py patch    # 1.0.0 -> 1.0.1
    python scripts/bump_version.py minor    # 1.0.0 -> 1.1.0  
    python scripts/bump_version.py major    # 1.0.0 -> 2.0.0
    python scripts/bump_version.py 1.2.3    # Set to specific version
"""

import argparse
import re
import sys
from pathlib import Path


def find_init_file():
    """Find the __init__.py file in the src directory."""
    src_dir = Path("src")
    if not src_dir.exists():
        raise FileNotFoundError("src/ directory not found")
    
    init_files = list(src_dir.glob("*/__init__.py"))
    if not init_files:
        raise FileNotFoundError("No __init__.py found in src/")
    
    return init_files[0]


def get_current_version(init_file):
    """Extract current version from __init__.py."""
    content = init_file.read_text()
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', content)
    if not match:
        raise ValueError("__version__ not found in __init__.py")
    return match.group(1)


def parse_version(version_str):
    """Parse version string into major, minor, patch components."""
    match = re.match(r'^(\d+)\.(\d+)\.(\d+)(?:[-.]?(?:a|b|rc|dev)\d*)?$', version_str)
    if not match:
        raise ValueError(f"Invalid version format: {version_str}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def bump_version(current_version, bump_type):
    """Bump version according to semver rules."""
    if re.match(r'^\d+\.\d+\.\d+', bump_type):
        # Specific version provided
        return bump_type
    
    major, minor, patch = parse_version(current_version)
    
    if bump_type == "major":
        return f"{major + 1}.0.0"
    elif bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    elif bump_type == "patch":
        return f"{major}.{minor}.{patch + 1}"
    else:
        raise ValueError(f"Invalid bump type: {bump_type}")


def update_init_file(init_file, new_version):
    """Update the version in __init__.py."""
    content = init_file.read_text()
    new_content = re.sub(
        r'(__version__\s*=\s*["\'])[^"\']+(["\'])',
        rf'\g<1>{new_version}\g<2>',
        content
    )
    init_file.write_text(new_content)


def main():
    parser = argparse.ArgumentParser(description="Bump version in __init__.py")
    parser.add_argument(
        "bump_type", 
        choices=["major", "minor", "patch"],
        nargs="?",
        help="Type of version bump or specific version (e.g., 1.2.3)"
    )
    parser.add_argument(
        "version",
        nargs="?", 
        help="Specific version to set (e.g., 1.2.3)"
    )
    
    args = parser.parse_args()
    
    # Handle the case where a specific version is provided as first argument
    if args.bump_type and re.match(r'^\d+\.\d+\.\d+', args.bump_type):
        bump_type = args.bump_type
    elif args.version:
        bump_type = args.version
    elif args.bump_type:
        bump_type = args.bump_type
    else:
        parser.print_help()
        sys.exit(1)
    
    try:
        init_file = find_init_file()
        current_version = get_current_version(init_file)
        
        print(f"Current version: {current_version}")
        
        new_version = bump_version(current_version, bump_type)
        print(f"New version: {new_version}")
        
        update_init_file(init_file, new_version)
        print(f"✓ Updated {init_file}")
        
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
