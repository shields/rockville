#!/bin/sh
#
# gitcalver.sh: derive version numbers from git history
#
# See https://gitcalver.org for details.
#
# Copyright © 2026 Michael Shields
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

set -eu

VERSION=""

usage() {
    cat <<'EOF'
Usage: gitcalver [OPTIONS] [REVISION | VERSION]

Derive a version number from git history using calendar versioning.

If REVISION is a git revision (commit, tag, branch), output its version.
If VERSION is a gitcalver version number, output the corresponding commit hash.
If neither is given, output the version for HEAD.
Use -- to separate options from a revision that starts with -.

Options:
  --prefix PREFIX     Literal string prepended to version (default: empty);
                      required to strip prefix in reverse lookup
  --dirty STRING      Enable dirty versions; append STRING.HASH to base
                      (STRING must not be empty)
  --no-dirty          Refuse dirty versions (overrides --dirty)
  --no-dirty-hash     Suppress .HASH suffix (requires --dirty)
  --branch BRANCH     Base branch name (e.g. "main"); overrides auto-detection
  --short             Output short commit hash (version-to-commit mode)
  --version           Show version information
  --help              Show this help

Exit codes:
  0   Success
  1   Error (not a git repo, no commits, decreasing dates, etc.)
  2   Dirty workspace or off default branch (without --dirty)
  3   Cannot trace to default branch
EOF
    exit 0
}

die() {
    printf 'gitcalver: %s\n' "$1" >&2
    exit "${2:-1}"
}

# --- Parse arguments ---

PREFIX=""
DIRTY_STRING=""
DIRTY_SET=false
NO_DIRTY=false
NO_DIRTY_HASH=false
BRANCH_OVERRIDE=""
POSITIONAL=""
SHORT_HASH=false

while [ $# -gt 0 ]; do
    case "$1" in
    --prefix)
        [ $# -ge 2 ] || die "--prefix requires an argument"
        PREFIX="$2"
        shift 2
        ;;
    --dirty)
        [ $# -ge 2 ] || die "--dirty requires an argument"
        [ -n "$2" ] || die "--dirty requires a non-empty argument"
        DIRTY_STRING="$2"
        DIRTY_SET=true
        shift 2
        ;;
    --no-dirty)
        NO_DIRTY=true
        shift
        ;;
    --no-dirty-hash)
        NO_DIRTY_HASH=true
        shift
        ;;
    --branch)
        [ $# -ge 2 ] || die "--branch requires an argument"
        BRANCH_OVERRIDE="$2"
        shift 2
        ;;
    --short)
        SHORT_HASH=true
        shift
        ;;
    --version)
        if [ -n "$VERSION" ]; then
            printf 'gitcalver %s\n' "$VERSION"
        else
            printf 'gitcalver (development)\n'
        fi
        exit 0
        ;;
    --help)
        usage
        ;;
    --)
        shift
        break
        ;;
    -*)
        die "unknown option: $1"
        ;;
    *)
        [ -z "$POSITIONAL" ] || die "unexpected argument: $1"
        POSITIONAL="$1"
        shift
        ;;
    esac
done

# Handle positional argument after --
if [ $# -gt 0 ]; then
    [ -z "$POSITIONAL" ] || die "unexpected argument: $1"
    POSITIONAL="$1"
    [ $# -le 1 ] || die "unexpected argument: $2"
fi

# Validate flag combinations
if $NO_DIRTY_HASH && ! $DIRTY_SET; then
    die "--no-dirty-hash requires --dirty"
fi

# --- Verify git repository ---

git rev-parse --git-dir >/dev/null 2>&1 ||
    die "not a git repository"

# --- Verify commits exist ---

git rev-parse HEAD >/dev/null 2>&1 ||
    die "no commits in repository"

# --- Reject shallow clones ---

if [ "$(git rev-parse --is-shallow-repository)" = "true" ]; then
    die "shallow clone detected; full history is required (use git fetch --unshallow)"
fi

# --- Determine and verify default branch ---

detect_default_branch() {
    # 1. Explicit override
    if [ -n "$BRANCH_OVERRIDE" ]; then
        printf '%s\n' "$BRANCH_OVERRIDE"
        return
    fi

    # 2. Remote default (origin/HEAD). Strip only the remote-tracking prefix,
    # not every path component: a branch name may itself contain slashes (e.g.
    # "release/v1"), and "${ref##*/}" would mangle it down to the last segment.
    local ref
    ref=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null) || true
    if [ -n "$ref" ]; then
        printf '%s\n' "${ref#refs/remotes/origin/}"
        return
    fi

    # 3. Check origin/main, then origin/master
    if git rev-parse --verify refs/remotes/origin/main >/dev/null 2>&1; then
        echo "main"
        return
    fi
    if git rev-parse --verify refs/remotes/origin/master >/dev/null 2>&1; then
        echo "master"
        return
    fi

    # 4. Check local main, then master
    if git rev-parse --verify refs/heads/main >/dev/null 2>&1; then
        echo "main"
        return
    fi
    if git rev-parse --verify refs/heads/master >/dev/null 2>&1; then
        echo "master"
        return
    fi

    return 1
}

DEFAULT_BRANCH=$(detect_default_branch) ||
    die "cannot determine default branch"

# Check if a commit is on the given branch (reachable from its tip).
commit_on_branch() {
    local rev="$1"
    local branch="$2"

    # Fast path: HEAD checked out on the branch. This is load-bearing,
    # not just an optimization. Without it, unpushed commits on main
    # would resolve branch_sha to origin/main (behind HEAD), the
    # --is-ancestor check would fail, and we'd fall into the off-branch
    # path and version the merge-base instead of HEAD.
    if [ "$rev" = "HEAD" ]; then
        local current
        current=$(git symbolic-ref --short HEAD 2>/dev/null) || true
        if [ "$current" = "$branch" ]; then
            return 0
        fi
    fi

    local rev_sha branch_sha
    rev_sha=$(git rev-parse --verify "$rev" 2>/dev/null) || return 1

    branch_sha=$(git rev-parse --verify "refs/remotes/origin/$branch" 2>/dev/null) ||
        branch_sha=$(git rev-parse --verify "refs/heads/$branch" 2>/dev/null) ||
        return 1

    git merge-base --is-ancestor "$rev_sha" "$branch_sha" 2>/dev/null
}

# Resolve the tip commit of the given branch (remote first, then local).
resolve_branch_tip() {
    local branch="$1"
    git rev-parse --verify "refs/remotes/origin/$branch" 2>/dev/null ||
        git rev-parse --verify "refs/heads/$branch" 2>/dev/null
}

# Match a bare YYYYMMDD.N version string.
# Outputs the version on success, produces no output on failure.
parse_gitcalver_version() {
    # A version is a single line. Reject embedded newlines up front: grep -x
    # matches any one line, so a multi-line argument could otherwise smuggle a
    # valid version line past it.
    case "$1" in
    *'
'*) return 0 ;;
    esac
    printf '%s\n' "$1" | grep -xE '[0-9]{8}\.[1-9][0-9]*' || true
}

# --- Reverse lookup (version → commit) ---

LOOKUP="$POSITIONAL"
if [ -n "$PREFIX" ] && [ -n "$LOOKUP" ]; then
    case "$LOOKUP" in
    "$PREFIX"*) LOOKUP="${LOOKUP#"$PREFIX"}" ;;
    esac
fi

CORE=$(parse_gitcalver_version "$LOOKUP")

if [ -n "$PREFIX" ] && [ -n "$CORE" ] && [ "$LOOKUP" = "$POSITIONAL" ]; then
    die "version $POSITIONAL is missing required prefix \"$PREFIX\""
fi

if [ -n "$CORE" ]; then
    TARGET_DATE=${CORE%%.*}
    TARGET_N=${CORE#*.}

    [ "$TARGET_N" -gt 0 ] 2>/dev/null ||
        die "invalid count in version: $POSITIONAL"

    BRANCH_TIP=$(resolve_branch_tip "$DEFAULT_BRANCH") ||
        die "cannot resolve default branch: $DEFAULT_BRANCH"

    # Walk first-parent history to find the commit
    FOUND=$(TZ=UTC git log "$BRANCH_TIP" --first-parent \
        --format='%H %cd' --date=format-local:'%Y%m%d' |
        awk -v td="$TARGET_DATE" -v tn="$TARGET_N" '
            $2 == td { hashes[++count] = $1; next }
            count > 0 { exit }
            $2 + 0 < td + 0 { exit }
            END {
                if (count > 0) {
                    idx = count - tn + 1
                    if (idx >= 1 && idx <= count) print hashes[idx]
                }
            }
        ')

    [ -n "$FOUND" ] || die "version not found: $POSITIONAL"

    if $SHORT_HASH; then
        git rev-parse --short "$FOUND"
    else
        printf '%s\n' "$FOUND"
    fi
    exit 0
fi

# --- Forward computation (revision → version) ---

if $SHORT_HASH; then
    die "--short is only valid in reverse lookup mode"
fi

if [ -n "$POSITIONAL" ]; then
    # --verify is required for safety: without it, git rev-parse echoes an
    # unrecognized option-like argument (e.g. "-foo") back unchanged and exits
    # 0, so the "validation" would pass and the attacker-controlled string would
    # flow on into git merge-base/git log as an option. --verify forces a single
    # resolved revision and rejects anything that is not one.
    REV=$(git rev-parse --verify "$POSITIONAL^{commit}" 2>/dev/null) ||
        die "not a gitcalver version or git revision: $POSITIONAL"
else
    REV=HEAD
fi

OFF_BRANCH=false
DIRTY_REV=HEAD
if ! commit_on_branch "$REV" "$DEFAULT_BRANCH"; then
    BRANCH_TIP=$(resolve_branch_tip "$DEFAULT_BRANCH") ||
        die "cannot resolve default branch: $DEFAULT_BRANCH"
    if [ -z "$POSITIONAL" ]; then
        MERGE_BASE=$(git merge-base HEAD "$BRANCH_TIP" 2>/dev/null) ||
            die "cannot trace HEAD to the default branch ($DEFAULT_BRANCH)" 3
    else
        DIRTY_REV="$REV"
        MERGE_BASE=$(git merge-base "$REV" "$BRANCH_TIP" 2>/dev/null) ||
            die "cannot trace $POSITIONAL to the default branch ($DEFAULT_BRANCH)" 3
    fi
    REV="$MERGE_BASE"
    OFF_BRANCH=true
fi

# --- Check dirty workspace (only for HEAD) ---

IS_DIRTY=false
if [ -z "$POSITIONAL" ]; then
    if $OFF_BRANCH || git status --porcelain 2>/dev/null | grep -q .; then
        IS_DIRTY=true
    fi
elif $OFF_BRANCH; then
    IS_DIRTY=true
fi

if $IS_DIRTY && { $NO_DIRTY || ! $DIRTY_SET; }; then
    if $OFF_BRANCH; then
        die "off the default branch ($DEFAULT_BRANCH)" 2
    else
        die "workspace is dirty" 2
    fi
fi

# --- Compute version ---

# Walk first-parent history: extract the commit's date and count consecutive
# same-day commits in a single git invocation.
read -r DATE COUNT PREV_DATE <<EOF
$(TZ=UTC git log "$REV" --first-parent --format='%cd' --date=format-local:'%Y%m%d' |
    awk '
        NR == 1 { d = $0; n = 1; next }
        $0 == d { n++; next }
        { print d, n, $0; exit }
        END { if (NR == n) print d, n, "" }
    ')
EOF

# Validate non-decreasing committer dates at the boundary. This only checks
# the transition between the current date block and the immediately preceding
# one; a non-monotonic sequence deeper in history (e.g. after a complex rebase)
# would not be caught here.
if [ -n "$PREV_DATE" ] && [ "$PREV_DATE" -gt "$DATE" ]; then
    die "committer dates go backwards (found $PREV_DATE after $DATE in history)"
fi

# --- Format output ---

VERSION="${PREFIX}${DATE}.${COUNT}"

if $IS_DIRTY; then
    if $NO_DIRTY_HASH; then
        printf '%s%s\n' "$VERSION" "$DIRTY_STRING"
    else
        HASH=$(git rev-parse --short "$DIRTY_REV")
        printf '%s%s.%s\n' "$VERSION" "$DIRTY_STRING" "$HASH"
    fi
else
    printf '%s\n' "$VERSION"
fi
