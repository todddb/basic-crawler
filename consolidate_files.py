#!/usr/bin/env python3
"""
Script to consolidate all text files in a project directory into a single file.
Useful for uploading entire codebases to AI assistants.
"""

import os
import sys
from pathlib import Path

def is_text_file(file_path):
    """Check if a file is likely a text file based on extension."""
    text_extensions = {
        '.py', '.js', '.html', '.css', '.json', '.md', '.txt', '.sh', '.yaml', '.yml',
        '.xml', '.sql', '.ini', '.cfg', '.conf', '.env', '.gitignore', '.patch'
    }
    
    # Files without extensions that are typically text
    text_filenames = {
        'README', 'LICENSE', 'Dockerfile', 'Makefile', 'requirements.txt'
    }
    
    file_path = Path(file_path)
    
    # Check by extension
    if file_path.suffix.lower() in text_extensions:
        return True
    
    # Check by filename
    if file_path.name in text_filenames:
        return True
    
    return False

def should_skip_directory(dir_name):
    """Check if a directory should be skipped."""
    skip_dirs = {
        'venv', '__pycache__', '.git', 'node_modules', '.pytest_cache',
        '.mypy_cache', 'dist', 'build', '.egg-info'
    }
    return dir_name in skip_dirs

def should_skip_file(file_path):
    """Check if a file should be skipped."""
    file_path = Path(file_path)
    
    # Skip large vendor files or minified files
    skip_patterns = {
        'socket.io.js',  # Large vendor file
        '.min.js',       # Minified files
        '.min.css'
    }
    
    # Skip log files (they can be large and change frequently)
    if file_path.suffix in ['.log']:
        return True
    
    # Skip if filename contains skip patterns
    for pattern in skip_patterns:
        if pattern in file_path.name:
            return True
    
    return False

def consolidate_files(root_dir='.', output_file='consolidated_project.txt'):
    """
    Walk through directory and consolidate all text files into one file.
    
    Args:
        root_dir (str): Root directory to start from
        output_file (str): Output file name
    """
    root_path = Path(root_dir).resolve()
    output_path = Path(output_file).resolve()
    
    print(f"Consolidating files from: {root_path}")
    print(f"Output file: {output_path}")
    
    files_processed = 0
    files_skipped = 0
    
    with open(output_path, 'w', encoding='utf-8') as outfile:
        # Write header
        outfile.write(f"# Consolidated Project Files\n")
        outfile.write(f"# Generated from: {root_path}\n")
        outfile.write(f"# Total files processed will be shown at the end\n\n")
        outfile.write("=" * 80 + "\n\n")
        
        # Walk through directory
        for root, dirs, files in os.walk(root_path):
            # Skip certain directories
            dirs[:] = [d for d in dirs if not should_skip_directory(d)]
            
            current_dir = Path(root)
            relative_dir = current_dir.relative_to(root_path)
            
            for file in sorted(files):
                file_path = current_dir / file
                relative_file_path = relative_dir / file
                
                # Skip certain files
                if should_skip_file(file_path):
                    files_skipped += 1
                    print(f"Skipped: {relative_file_path}")
                    continue
                
                # Only process text files
                if not is_text_file(file_path):
                    files_skipped += 1
                    print(f"Skipped (binary): {relative_file_path}")
                    continue
                
                try:
                    # Read file content
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as infile:
                        content = infile.read()
                    
                    # Write file header and content
                    outfile.write(f"{'=' * 80}\n")
                    outfile.write(f"FILE: {relative_file_path}\n")
                    outfile.write(f"{'=' * 80}\n\n")
                    outfile.write(content)
                    outfile.write(f"\n\n{'=' * 80}\n")
                    outfile.write(f"END OF FILE: {relative_file_path}\n")
                    outfile.write(f"{'=' * 80}\n\n\n")
                    
                    files_processed += 1
                    print(f"Processed: {relative_file_path}")
                    
                except Exception as e:
                    files_skipped += 1
                    print(f"Error reading {relative_file_path}: {e}")
                    outfile.write(f"{'=' * 80}\n")
                    outfile.write(f"ERROR READING FILE: {relative_file_path}\n")
                    outfile.write(f"Error: {e}\n")
                    outfile.write(f"{'=' * 80}\n\n")
        
        # Write footer with statistics
        outfile.write(f"\n{'=' * 80}\n")
        outfile.write(f"CONSOLIDATION COMPLETE\n")
        outfile.write(f"{'=' * 80}\n")
        outfile.write(f"Files processed: {files_processed}\n")
        outfile.write(f"Files skipped: {files_skipped}\n")
        outfile.write(f"Total files: {files_processed + files_skipped}\n")
        outfile.write(f"{'=' * 80}\n")
    
    print(f"\nConsolidation complete!")
    print(f"Files processed: {files_processed}")
    print(f"Files skipped: {files_skipped}")
    print(f"Output saved to: {output_path}")

if __name__ == "__main__":
    # Parse command line arguments
    if len(sys.argv) > 2:
        root_dir = sys.argv[1]
        output_file = sys.argv[2]
    elif len(sys.argv) > 1:
        root_dir = sys.argv[1]
        output_file = 'consolidated_project.txt'
    else:
        root_dir = '.'
        output_file = 'consolidated_project.txt'
    
    consolidate_files(root_dir, output_file)
