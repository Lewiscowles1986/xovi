#!/usr/bin/python3
import re
from argparse import ArgumentParser
from os import makedirs
from shutil import rmtree, copy
from dataclasses import dataclass
from os.path import join, dirname, splitext, basename, isfile

EXPORT = 1
IMPORT = 2
OVERRIDE = 3
COPY = 4
VERSION = 5
RESOURCE = 6
MODULEBASE = 7
CONDITION = 8

@dataclass
class Directive:
    dir_type: int
    symbol: str
    file: str

@dataclass
class DirectiveFile:
    file_set: set
    modulebase: str
    global_directives: list
    file_directives: dict
    problems: list
    make_files: list

@dataclass
class Problem:
    error: bool
    line_number: int
    line_contents: str
    message: str

    def __repr__(self):
        t = "Error" if self.error else "Warning"
        return f"{t}: at line: {self.line_number}: {self.line_contents} - {self.message}"

def parse_directives(directives):
    # Every line should follow the format <file | global | import | export | override> <object>
    final_dirs = {"": []}
    final_file_set = set()

    problems = []
    current_file = ""
    modulebase = None
    make_files = []
    for i, directive in enumerate(directives):
        # Remove comments:
        if ';' in directive:
            directive = directive[:directive.find(';')]
        tokens = [x for x in directive.split() if x]
        l = len(tokens)
        if l == 0: continue

        if tokens[0].lower() == 'global':
            current_file = ""
            continue

        if l > 2:
            problems.append(Problem(False, i + 1, directive, "Additional data found when parsing directive"))
        if l < 2:
            problems.append(Problem(True, i + 1, directive, "Illegal line."))
            break

        directive, operand = tokens
        out_directive = None
        out_directives = []
        match directive.lower():
            case 'file':
                current_file = operand
                final_file_set.add(current_file)
                final_dirs[current_file] = []
            case 'make':
                make_files.append(operand)
            case 'export':
                out_directive = Directive(EXPORT, operand, current_file)
            case 'import':
                out_directive = Directive(IMPORT, operand, current_file)
            case 'import?':
                out_directives = [
                    Directive(IMPORT, operand, current_file),
                    Directive(CONDITION, operand, '')
                ]
            case 'condition':
                out_directive = Directive(CONDITION, operand, '')
            case 'override':
                out_directive = Directive(OVERRIDE, operand, current_file)
            case 'copy':
                out_directive = Directive(COPY, operand, '')
            case 'version':
                out_directive = Directive(VERSION, operand, '')
            case 'resource':
                out_directive = Directive(RESOURCE, operand, '')
            case 'modulebase':
                if modulebase is not None:
                    problems.append(Problem(True, i + 1, directive, "Cannot specify more than one module base"))
                    break
                modulebase = operand
                current_file = modulebase
                final_file_set.add(modulebase)
                final_dirs[modulebase] = []
        if out_directives:
            for d in out_directives:
                final_dirs[d.file].append(d)
        if out_directive:
            final_dirs[out_directive.file].append(out_directive)
    gl = final_dirs[""]
    unloaded_make_files = [x for x in make_files if x not in final_dirs]
    if unloaded_make_files:
        for x in unloaded_make_files:
            print(f"Warning: File {x} is not included, but is required to be made.")
    del final_dirs[""]
    return DirectiveFile(
        final_file_set,
        modulebase,
        gl,
        final_dirs,
        problems,
        make_files,
    )

def process(root, outdir, dirtree, cpp):
    any_args = "..." if cpp else ""
    # iterate over affected files
    final_out_nametable = []
    final_out_vartable = []
    input_import_lookups = ['ILLEGAL']
    externs = []
    next_import_idx = 1
    for copy_directive in filter(lambda e: e.dir_type == COPY, dirtree.global_directives):
        fname = copy_directive.symbol
        makedirs(join(outdir, dirname(fname)), exist_ok=True)
        full_in_fname = join(root, fname)
        full_out_fname = join(outdir, fname)
        copy(full_in_fname, full_out_fname)

    resource_directives = [list(x.symbol.split(':')) for x in dirtree.global_directives if x.dir_type == RESOURCE]
    for res in resource_directives:
        if len(res) != 2:
            print(f"Error: Resource directive should follow the format `resname:/path/to/resource`")
            return
        if not isfile(res[1]):
            print(f"Error: '{res[1]}' (resource {res[0]}) is not a file!")
            return
        with open(res[1], 'rb') as f:
            res[1] = f.read() + b'\0'

    module_base_contents = ""
    for fname in dirtree.file_set:
        all_directives = [*dirtree.global_directives, *dirtree.file_directives[fname]]
        makedirs(join(root, dirname(join(outdir, fname))), exist_ok=True)
        full_in_fname = join(root, fname)
        full_out_fname = join(outdir, fname)
        with open(full_in_fname, 'r') as read:
            input_contents = read.read()
            for directive in all_directives:
                if directive.dir_type == CONDITION and f"C{directive.symbol}" not in final_out_nametable:
                    final_out_vartable.append('0')
                    final_out_nametable.append(f"C{directive.symbol}")
                    next_import_idx += 1
                elif directive.dir_type == IMPORT:
                    # have we already imported this?
                    if directive.symbol in input_import_lookups:
                        # Yes - use that
                        idx = input_import_lookups.index(directive.symbol)
                    else:
                        # No, define it
                        input_import_lookups.append(directive.symbol)
                        idx = next_import_idx
                        next_import_idx += 1
                        final_out_vartable.append('0')
                        final_out_nametable.append(f"I{directive.symbol}")
                    new_name = f'((unsigned long long int(*)({any_args})) LINKTABLEVALUES[{idx}])'
                    if '$' not in directive.symbol:
                        name = rf"(?<!override)\${re.escape(directive.symbol).replace('-', '_')}"
                        input_contents = re.sub(name, new_name, input_contents)
                    else:
                        name = directive.symbol
                        input_contents = input_contents.replace(name.replace('-', '_'), new_name)
                elif directive.dir_type == EXPORT:
                    next_import_idx += 1
                    externs.append(directive.symbol)
                    final_out_vartable.append(directive.symbol)
                    final_out_nametable.append(f'E{directive.symbol}')
                elif directive.dir_type == OVERRIDE:
                    next_import_idx += 1
                    externs.append(f"override${directive.symbol}")
                    final_out_vartable.append(f"override${directive.symbol}")
                    final_out_nametable.append(f'O{directive.symbol}')
            output =  'extern const void *LINKTABLEVALUES[];\n'
            for res in resource_directives:
                output += f'extern const char r${res[0]}[{len(res[1])}];\n'
            output += input_contents
            if dirtree.modulebase != fname:
                with open(full_out_fname, 'w') as write:
                    write.write(output)
            else:
                module_base_contents = output
    version_directives = [x for x in dirtree.global_directives if x.dir_type == VERSION]
    if len(version_directives) > 1:
        print("Warning: More than one version directive in the XOVI project file.")
    if not version_directives:
        print("Warning: No version defined in the XOVI project file.")
        version = None
    else:
        version_string = version_directives[0].symbol
        try:
            version_tokens = [int(x) for x in version_string.strip().split('.')]
            if len(version_tokens) != 3 or any(x > 255 or x < 0 for x in version_tokens):
                raise BaseException("invalid format")
        except BaseException:
            print(f"Warning: Invalid version format {version_string}. Use major.minor.patch (semver). Assuming 0.1.0")
            version_tokens = [0, 1, 0]
        version = (version_tokens[0] << 16) | (version_tokens[1] << 8) | (version_tokens[2])

    with open(join(outdir, 'xovi.c'), 'w') as linktable:
        zero = '\\0'
        nl = '\n'
        final_out_vartable.insert(0, str(len(final_out_vartable)))
        linktable.write(f"""// This file is autogenerated. Please do not alter it manually and instead run xovigen.py.{nl}""")
        linktable.write(module_base_contents)
        linktable.write("\n")
        for extern in externs:
            linktable.write(f"extern void {extern}();\n")
        vartable = [f'(void *) {x}' for x in final_out_vartable]
        linktable.write(f"""__attribute__((section(".xovi"))) const char *LINKTABLENAMES = "{zero.join(final_out_nametable)}{zero}{zero}";{nl}""")
        linktable.write(f"""__attribute__((section(".xovi"))) const void *LINKTABLEVALUES[] = {{{', '.join(vartable)}}};{nl}""")
        linktable.write(f"""__attribute__((section(".xovi"))) const struct XoViEnvironment *Environment = 0;{nl}""")

        for res in resource_directives:
            linktable.write(f"""const char r${res[0]}[{len(res[1])}] = {{ {','.join(map(str, res[1]))} }};{nl}""")

        if version is not None:
            linktable.write(f"""__attribute__((section(".xovi_info"))) const int EXTENSIONVERSION = {version};{nl}""")

def write_make_file(output_root, project_name, compiler, arguments, directives):
    with open(join(output_root, 'make.sh'), 'w') as out:
        out.write('#!/bin/bash\n')
        files = ' '.join([f'"{join(output_root, x)}"' for x in directives.make_files])
        output_so = join(output_root, project_name + '.so')
        out.write(f'{compiler} -fPIC -shared {arguments} -o {output_so} {files}\n')

def main():
    argparse = ArgumentParser()
    argparse.add_argument('-o', '--output', help="Output directory (WILL BE DELETED)", required=True)
    argparse.add_argument('-p', '--cpp', help="Process C++ instead", action='store_true')
    argparse.add_argument('-r', '--root', help="Root of the files described in XoVi config")
    argparse.add_argument('-m', '--write-make', help="Root of the files described in XoVi config", action='store_true')
    argparse.add_argument('-c', '--compiler', help="C compiler command", default='gcc')
    argparse.add_argument('-a', '--compiler-arguments', help="Additional arguments to provide to the C compiler", default='')
    argparse.add_argument('input', help="The .xovi file defining all imports and exports of all the files in this project.")
    args = argparse.parse_args()

    rmtree(args.output, ignore_errors=True)
    makedirs(args.output, exist_ok=True)

    with open(args.input, 'r') as definition:
        directives = [x.strip() for x in definition.readlines()]
    parsed = parse_directives(directives)
    for problem in parsed.problems:
        print(problem)
        if problem.error:
            return
    process(args.root if args.root else dirname(args.input), args.output, parsed, args.cpp)
    if args.write_make and len(parsed.make_files):
        if 'xovi.c' not in parsed.make_files:
            parsed.make_files.append('xovi.c')
        write_make_file(
            args.output,
            splitext(basename(args.input))[0],
            args.compiler,
            args.compiler_arguments,
            parsed
        )


if __name__ == "__main__": main()
