// The hub, ported to Go.
//
// Go 1.20 is the version this port was built and tested with (`go.mod` also pins the
// language semantics: a `go 1.18` directive would silently keep pre-1.22 loop-variable
// scoping on a 1.22+ toolchain, and this code passes its loop variables explicitly
// rather than relying on either behaviour).
//
// The module path is the project name rather than a URL because nothing here is
// fetched: this port has **no third-party dependencies at all**. The Unicode
// tables are generated from CPython (`tools/gen_unicode_tables.py`), the SQLite
// binding is cgo against the system library, and the HTTP server is `net/http`.
// That is not minimalism for its own sake -- the sandbox this port was written in
// can reach github.com and pypi.org and nothing else, so `go mod download`
// cannot resolve anything, and a dependency-free module is one that builds
// anywhere the toolchain does.
module amdhub

go 1.20
