#!/usr/bin/env python3
"""Analyze Python files for dead code patterns."""

import ast
import os
import re
from pathlib import Path
from typing import Set, Dict, List, Tuple

def get_imports(tree: ast.AST) -> Dict[str, List[Tuple[int, str]]]:
    """Extract all imports and their line numbers."""
    imports = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name
                imports[name] = [(node.lineno, alias.name)]
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = alias.asname or alias.name
                imports[name] = [(node.lineno, f"{node.module}.{alias.name}")]
    return imports

def get_all_names(tree: ast.AST) -> Set[str]:
    """Get all names referenced in the code."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                names.add(node.value.id)
    return names

def find_unused_imports(filepath: str) -> List[Dict]:
    """Find unused imports in a Python file."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        tree = ast.parse(content)
        imports = get_imports(tree)
        used_names = get_all_names(tree)
        
        unused = []
        for import_name, import_info in imports.items():
            if import_name not in used_names and not import_name.startswith('_'):
                unused.append({
                    'line': import_info[0][0],
                    'import': import_info[0][1],
                    'used_as': import_name
                })
        
        return unused
    except (SyntaxError, UnicodeDecodeError):
        return []

def find_empty_files(root_dir: str) -> List[str]:
    """Find empty or nearly empty Python files."""
    empty = []
    for filepath in Path(root_dir).glob('**/*.py'):
        if filepath.name.startswith('__'):
            continue
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = [l.strip() for l in f if l.strip() and not l.strip().startswith('#')]
        if len(lines) == 0:
            empty.append(str(filepath))
    return empty

def main():
    root = 'bigdata_briefs'
    
    print("=" * 80)
    print("DEAD CODE ANALYSIS")
    print("=" * 80)
    
    # Find files with unused imports
    print("\n1. FILES WITH UNUSED IMPORTS:")
    print("-" * 80)
    
    files_with_unused = {}
    for filepath in Path(root).glob('**/*.py'):
        if filepath.name.startswith('__'):
            continue
        unused = find_unused_imports(str(filepath))
        if unused:
            rel_path = str(filepath).replace('.\', '').replace('\', '/')
            files_with_unused[rel_path] = unused
    
    for fpath in sorted(files_with_unused.keys())[:15]:
        print(f"\n{fpath}")
        for u in files_with_unused[fpath][:3]:
            print(f"  Line {u['line']:3d}: {u['import']:40s} (used as: {u['used_as']})")
    
    if len(files_with_unused) > 15:
        print(f"\n... and {len(files_with_unused) - 15} more files")
    
    print(f"\nTotal files with unused imports: {len(files_with_unused)}")
    
    # Find empty files
    print("\n\n2. EMPTY OR NEAR-EMPTY FILES:")
    print("-" * 80)
    empty = find_empty_files(root)
    for fpath in empty:
        print(fpath.replace('.\', ''))
    
    if empty:
        print(f"\nTotal empty files: {len(empty)}")
    else:
        print("None found.")

if __name__ == '__main__':
    main()
