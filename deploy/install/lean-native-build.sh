#!/usr/bin/env bash
# lean-native-build.sh OUT_DIRECTORY - compile linarith's search step into one library.
#
# Runs INSIDE a container of the Lean server's image (pool-lib.sh, install_compiled_search), in the
# workspace the server uses (LEAN_SERVER_PROJECT_DIR, which the image sets). Nothing is downloaded: the C
# files are the ones Mathlib's build cache already left in the image beside its .olean files, and the
# compiler is the Lean toolchain's own (`leanc`). So the library is a function of the image, of this
# script and of the list beside it (`lean-native-modules`), and every box makes its own from its own copy
# of the image.
#
# It writes into OUT_DIRECTORY:
#   linarith-search.so   the modules named in `lean-native-modules`, as native code
#   sources              the files it was compiled from, one path a line
#   built-from           one hash over those files; the launcher takes it again before it loads the library
#
# Two things about the library are easy to get wrong, and both are checked below.
#   1. A compiled module calls the initializer of every module it imports. The imports that are NOT
#      compiled here get an initializer that does nothing, HIDDEN inside the library: it only satisfies
#      the call. Were it visible, Lean would take that module for a compiled one and skip the module's own
#      start-up code (for Mathlib.Tactic.Linarith.Datatypes, the registration of linarith's trace class).
#   2. The library is preloaded into the REPL (LD_PRELOAD), and an environment variable is inherited by
#      every program that process starts. The library holds references to Lean's own data, which only a
#      Lean process has: any other program would refuse to start with it. So the library takes LD_PRELOAD
#      out of the environment the moment it is loaded.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
OUT=${1:?usage: lean-native-build.sh OUT_DIRECTORY}
PROJECT=${LEAN_SERVER_PROJECT_DIR:-}
[ -n "$PROJECT" ] || { echo "lean-native-build.sh: LEAN_SERVER_PROJECT_DIR is not set (this runs in the image of the Lean server, which sets it)" >&2; exit 2; }
[ -d "$OUT" ] || { echo "lean-native-build.sh: no directory $OUT" >&2; exit 2; }
OUT=$(cd "$OUT" && pwd)
cd "$PROJECT"

sources=()
wanted=()
while read -r package module rest; do
  case "$package" in ""|"#"*) continue ;; esac
  [ -n "$module" ] && [ -z "$rest" ] || { echo "lean-native-build.sh: a line of $HERE/lean-native-modules is not 'PACKAGE MODULE/PATH': $package $module $rest" >&2; exit 2; }
  file=$PROJECT/.lake/packages/$package/.lake/build/ir/$module.c
  [ -f "$file" ] || { echo "lean-native-build.sh: this image holds no C file for $module of $package ($file): nothing was built" >&2; exit 3; }
  case "$file" in *[[:space:]]*) echo "lean-native-build.sh: a path with white space: $file" >&2; exit 2 ;; esac
  sources+=("$file")
  wanted+=("initialize_${package}_${module//\//_}")
done < "$HERE/lean-native-modules"
[ "${#sources[@]}" -ge 1 ] || { echo "lean-native-build.sh: $HERE/lean-native-modules names no module" >&2; exit 2; }
[ -f "$PROJECT/lean-toolchain" ] || { echo "lean-native-build.sh: no $PROJECT/lean-toolchain" >&2; exit 3; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
library=$work/linarith-search.so

# What the toolchain's own libraries export: an initializer among these belongs to Lean, and is not ours
# to replace.
libdir=$(lake env lean --print-libdir)
{ nm -D --defined-only "$libdir"/*.so 2>/dev/null || true; } | awk '{print $NF}' | sort -u > "$work/provided"

stubs() {   # stubs FILE-OF-NAMES : the C file of point 1 above, with a do-nothing initializer for each name
  echo '#include <lean/lean.h>'
  echo 'extern int unsetenv(const char *name);'
  echo '__attribute__((constructor)) static void lean_native_forget_preload(void) { unsetenv("LD_PRELOAD"); }'
  while read -r name; do
    echo "__attribute__((visibility(\"hidden\"))) lean_object* $name(uint8_t builtin) { return lean_io_result_mk_ok(lean_box(0)); }"
  done < "$1"
}

# -DLEAN_EXPORTING: leanc hides every symbol otherwise, and Lean could not find the functions by name.
# -z now: every reference is resolved when the library is loaded, so one that cannot be is found out as
# the worker starts (and by lean-native-check.py before that), never in the middle of somebody's proof.
compile() {
  lake env leanc -shared -O3 -DNDEBUG -DLEAN_EXPORTING -Wl,-z,now -o "$library" "$work/stubs.c" "${sources[@]}"
}

: > "$work/stubbed"
for _ in 1 2 3 4; do
  stubs "$work/stubbed" > "$work/stubs.c"
  compile
  nm -D --undefined-only "$library" | awk '{print $NF}' | sort -u > "$work/undefined"
  { comm -23 "$work/undefined" "$work/provided" | grep -E '^initialize_' || true; } > "$work/missing"
  [ -s "$work/missing" ] || break
  sort -u "$work/stubbed" "$work/missing" > "$work/stubbed.next"
  mv "$work/stubbed.next" "$work/stubbed"
done
[ ! -s "$work/missing" ] || { echo "lean-native-build.sh: initializers still missing after four rounds:" >&2; cat "$work/missing" >&2; exit 4; }

# The library must export the initializer of each module it was asked for, and of no other module.
nm -D --defined-only "$library" | awk '{print $NF}' | grep -E '^initialize_' | sort -u > "$work/exported"
printf '%s\n' "${wanted[@]}" | sort -u > "$work/wanted"
if ! cmp -s "$work/exported" "$work/wanted"; then
  echo "lean-native-build.sh: the library does not export exactly the initializers of the modules asked for." >&2
  echo "  asked for and missing:" >&2; comm -13 "$work/exported" "$work/wanted" | sed 's/^/    /' >&2
  echo "  exported and not asked for:" >&2; comm -23 "$work/exported" "$work/wanted" | sed 's/^/    /' >&2
  exit 4
fi

printf '%s\n' "${sources[@]}" "$PROJECT/lean-toolchain" > "$work/sources"
# shellcheck disable=SC2046  # the same words lean-native-repl hashes
sha256sum $(cat "$work/sources") | sha256sum | cut -d ' ' -f 1 > "$work/built-from"

# `built-from` goes away first and comes back last: a worker that starts in between finds no hash and runs
# the plain REPL, never a new library under an old hash.
rm -f "$OUT/built-from"
install -m 0644 "$library" "$OUT/linarith-search.so.new"
mv -f "$OUT/linarith-search.so.new" "$OUT/linarith-search.so"
install -m 0644 "$work/sources" "$OUT/sources"
install -m 0644 "$work/built-from" "$OUT/built-from"

echo "COMPILED: ${#sources[@]} modules into $OUT/linarith-search.so ($(stat -c %s "$OUT/linarith-search.so") bytes); $(wc -l < "$work/stubbed") imported modules are not compiled and got an initializer that does nothing"
sed 's/^/  not compiled: /' "$work/stubbed"
