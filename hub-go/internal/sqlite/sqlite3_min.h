/* The slice of SQLite's C API this port uses, declared here because the build
 * environment has no `sqlite3.h`: `libsqlite3-0` is installed as a runtime
 * dependency of CPython's own `sqlite3` module, and the -dev package that would
 * carry the header is not, and cannot be fetched (deb.debian.org is unreachable
 * from this sandbox).
 *
 * Declaring the prototypes rather than changing the dependency is the right
 * shape for two reasons. The ABI is frozen: these functions have had the same
 * signatures for the whole life of the format, and SQLite's own amalgamation
 * declares them exactly like this. And the alternative -- vendoring the 8 MiB
 * amalgamation -- would put a generated C file in the repository to avoid one
 * link flag, when the deployment image already ships the library for Python.
 *
 * Only what is used is declared. A prototype that is wrong here would not
 * compile or would crash, and there is no partially-declared function in the
 * list below. */
#ifndef AMDHUB_SQLITE3_MIN_H
#define AMDHUB_SQLITE3_MIN_H

#ifdef __cplusplus
extern "C" {
#endif

typedef struct sqlite3 sqlite3;
typedef struct sqlite3_stmt sqlite3_stmt;
typedef void (*sqlite3_destructor_type)(void *);

/* Result codes. */
#define SQLITE_OK 0
#define SQLITE_ROW 100
#define SQLITE_DONE 101
#define SQLITE_BUSY 5
#define SQLITE_MISUSE 21
#define SQLITE_CONSTRAINT 19
#define SQLITE_CONSTRAINT_UNIQUE 2067

/* Open flags. */
#define SQLITE_OPEN_READONLY 0x00000001
#define SQLITE_OPEN_READWRITE 0x00000002
#define SQLITE_OPEN_CREATE 0x00000004
#define SQLITE_OPEN_URI 0x00000040
#define SQLITE_OPEN_FULLMUTEX 0x00010000

/* Column types. */
#define SQLITE_INTEGER 1
#define SQLITE_FLOAT 2
#define SQLITE_TEXT 3
#define SQLITE_BLOB 4
#define SQLITE_NULL 5

int sqlite3_open_v2(const char *filename, sqlite3 **ppDb, int flags, const char *zVfs);
int sqlite3_close(sqlite3 *db);
int sqlite3_close_v2(sqlite3 *db);
const char *sqlite3_errmsg(sqlite3 *db);
int sqlite3_errcode(sqlite3 *db);
int sqlite3_extended_errcode(sqlite3 *db);
int sqlite3_exec(
    sqlite3 *db, const char *sql,
    int (*callback)(void *, int, char **, char **), void *arg, char **errmsg);
void sqlite3_free(void *p);
int sqlite3_busy_timeout(sqlite3 *db, int ms);

int sqlite3_prepare_v2(
    sqlite3 *db, const char *zSql, int nByte, sqlite3_stmt **ppStmt, const char **pzTail);
int sqlite3_step(sqlite3_stmt *pStmt);
int sqlite3_reset(sqlite3_stmt *pStmt);
int sqlite3_clear_bindings(sqlite3_stmt *pStmt);
int sqlite3_finalize(sqlite3_stmt *pStmt);

int sqlite3_bind_int64(sqlite3_stmt *pStmt, int i, long long v);
int sqlite3_bind_double(sqlite3_stmt *pStmt, int i, double v);
int sqlite3_bind_null(sqlite3_stmt *pStmt, int i);
int sqlite3_bind_text(sqlite3_stmt *pStmt, int i, const char *z, int n,
                      sqlite3_destructor_type destroy);

int sqlite3_column_count(sqlite3_stmt *pStmt);
const char *sqlite3_column_name(sqlite3_stmt *pStmt, int iCol);
int sqlite3_column_type(sqlite3_stmt *pStmt, int iCol);
long long sqlite3_column_int64(sqlite3_stmt *pStmt, int iCol);
double sqlite3_column_double(sqlite3_stmt *pStmt, int iCol);
const unsigned char *sqlite3_column_text(sqlite3_stmt *pStmt, int iCol);
int sqlite3_column_bytes(sqlite3_stmt *pStmt, int iCol);

int sqlite3_changes(sqlite3 *db);
long long sqlite3_last_insert_rowid(sqlite3 *db);

/* `sqlite3_bind_text` copies the string only when handed SQLITE_TRANSIENT, which
 * is `(sqlite3_destructor_type)-1`. A cast of a function-pointer type from an
 * integer is not something cgo will write, and a Go string's backing array moves
 * (and is freed by the garbage collector), so the copy is mandatory rather than
 * an optimisation: binding without it would hand SQLite a pointer into the Go
 * heap that nothing pins. */
static void sqlite_bind_text_copy(sqlite3_stmt *pStmt, int i, const char *z, int n) {
  sqlite3_bind_text(pStmt, i, z, n, (sqlite3_destructor_type)-1);
}

#ifdef __cplusplus
}
#endif

#endif /* AMDHUB_SQLITE3_MIN_H */
