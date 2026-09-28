// Package sqlite is the cgo binding the job store is written against: a `Conn`,
// prepared statements, and typed bind/column access, over the system
// `libsqlite3.so.0`.
//
// It is deliberately small and deliberately *not* a `database/sql` driver. The
// store's SQL is three statements long, its concurrency story is `busy_timeout`
// plus SQLite's own write lock, and `database/sql` would add a connection pool
// whose shape (`MaxOpenConns`, per-connection pragmas, per-connection
// transactions) is exactly the state this port has to keep explicit to stay a
// faithful port of `hub/jobs.py`.
//
// **One `Conn` is one SQLite connection, and it is safe to use from several
// goroutines.** The library is opened with `SQLITE_OPEN_FULLMUTEX`, so SQLite
// serialises calls on that handle itself. That matters because the Python
// original's contract is one store per thread and its concurrency test uses
// twelve of them against one file -- SQLite's locking, not a mutex here, is what
// makes `claim_next` exclusive, and hiding it behind a Go mutex would make the
// test pass while proving nothing.
package sqlite

/*
#cgo LDFLAGS: -l:libsqlite3.so.0
#include <stdlib.h>
#include "sqlite3_min.h"
*/
import "C"

import (
	"errors"
	"fmt"
	"runtime"
	"unsafe"
)

// Result is a SQLite result code. The two the store cares about are beyond the
// API boundary: `Constraint` is how a duplicate is reported, and `Busy` is what
// `busy_timeout` is there to make rare.
type Result int

const (
	OK         Result = C.SQLITE_OK
	Row        Result = C.SQLITE_ROW
	Done       Result = C.SQLITE_DONE
	Busy       Result = C.SQLITE_BUSY
	Constraint Result = C.SQLITE_CONSTRAINT
)

// Error is a SQLite error, carrying both the result code and the message.
//
// The message is the *database's* text, not a paraphrase, because the store
// identifies a duplicate by reading it: SQLite reports a violated UNIQUE
// constraint by naming the columns, and `job_active_dedupe` is recognised from
// that string. See `jobs.isDedupeViolation` for why that is a positive
// identification rather than an inference.
type Error struct {
	Code    Result
	Extended int
	Message string
}

func (e *Error) Error() string {
	return fmt.Sprintf("sqlite: %s (code %d, extended %d)", e.Message, e.Code, e.Extended)
}

// Conn is one open database handle.
type Conn struct {
	db     *C.sqlite3
	closed bool
}

// Open opens (and creates) a database file.
//
// The flags are Python's `sqlite3.connect(path)` equivalent: read-write, create
// if absent, no URI interpretation. `FullMutex` is not Python's default
// (`check_same_thread` is) but is the same guarantee expressed the way this port
// needs it.
func Open(path string) (*Conn, error) {
	cpath := C.CString(path)
	defer C.free(unsafe.Pointer(cpath))

	var db *C.sqlite3
	flags := C.int(C.SQLITE_OPEN_READWRITE | C.SQLITE_OPEN_CREATE | C.SQLITE_OPEN_FULLMUTEX)
	if rc := C.sqlite3_open_v2(cpath, &db, flags, nil); rc != C.SQLITE_OK {
		message := "unable to open database file"
		if db != nil {
			message = C.GoString(C.sqlite3_errmsg(db))
			C.sqlite3_close_v2(db)
		}
		return nil, &Error{Code: Result(rc), Message: message}
	}
	return &Conn{db: db}, nil
}

// Close releases the handle. Idempotent, like the Python `close`.
func (c *Conn) Close() error {
	if c.closed {
		return nil
	}
	c.closed = true
	if rc := C.sqlite3_close_v2(c.db); rc != C.SQLITE_OK {
		return &Error{Code: Result(rc), Message: C.GoString(C.sqlite3_errmsg(c.db))}
	}
	runtime.SetFinalizer(c, nil)
	return nil
}

// BusyTimeout is `PRAGMA busy_timeout` expressed as the C call it is: how long a
// contended write waits before SQLite gives up. The shape of the failure when it
// does is a `Busy` error rather than a silently lost write.
func (c *Conn) BusyTimeout(ms int) error {
	if rc := C.sqlite3_busy_timeout(c.db, C.int(ms)); rc != C.SQLITE_OK {
		return c.err(rc)
	}
	return nil
}

// Exec runs one statement and discards its rows, which is what every
// `PRAGMA`/`CREATE TABLE`/`CREATE INDEX` in this port needs.
func (c *Conn) Exec(sql string) error {
	_, err := c.Query(sql)
	return err
}

// Query runs one statement and materialises every row.
//
// Materialised rather than streamed, and that is a deliberate simplification: the
// queue holds thousands of rows at most, the caller in every case wants all of
// them, and a streaming API would have to answer what happens to the statement
// when the caller stops early. `claim_next`'s `UPDATE ... RETURNING *` and the
// `SELECT`s share this one path, so a bug in row decoding cannot be one that only
// affects part of the store.
//
// Values come back as `nil`, `int64`, `float64` or `string` -- the four type
// classes SQLite's dynamic typing produces for this schema. A BLOB is returned as
// its bytes, because the alternative is an error for a column that does not
// exist.
func (c *Conn) Query(sql string, args ...any) (*Rows, error) {
	stmt, err := c.prepare(sql)
	if err != nil {
		return nil, err
	}
	defer stmt.finalize()

	if err := stmt.bind(args); err != nil {
		return nil, err
	}
	return stmt.rows()
}

// ExecArgs runs one statement with arguments and reports the row count and the
// last inserted row id, which is what `create_batch` needs from an INSERT and
// `mark` checks against zero.
func (c *Conn) ExecArgs(sql string, args ...any) (changes int64, lastID int64, err error) {
	stmt, err := c.prepare(sql)
	if err != nil {
		return 0, 0, err
	}
	defer stmt.finalize()

	if err := stmt.bind(args); err != nil {
		return 0, 0, err
	}
	if _, err := stmt.rows(); err != nil {
		return 0, 0, err
	}
	return int64(C.sqlite3_changes(c.db)), int64(C.sqlite3_last_insert_rowid(c.db)), nil
}

// Rows is a materialised result set.
type Rows struct {
	Columns []string
	Values  [][]any
}

// Len is the row count.
func (r *Rows) Len() int { return len(r.Values) }

// Row returns one row, or nil past the end -- the `fetchone()` shape.
func (r *Rows) Row(i int) []any {
	if i < 0 || i >= len(r.Values) {
		return nil
	}
	return r.Values[i]
}

func (c *Conn) prepare(sql string) (*Stmt, error) {
	csql := C.CString(sql)
	defer C.free(unsafe.Pointer(csql))

	var handle *C.sqlite3_stmt
	rc := C.sqlite3_prepare_v2(c.db, csql, C.int(len(sql)), &handle, nil)
	if rc != C.SQLITE_OK {
		return nil, c.err(rc)
	}
	return &Stmt{conn: c, handle: handle}, nil
}

func (c *Conn) err(rc C.int) error {
	return &Error{
		Code:     Result(rc),
		Extended: int(C.sqlite3_extended_errcode(c.db)),
		Message:  C.GoString(C.sqlite3_errmsg(c.db)),
	}
}

// Stmt is one prepared statement.
type Stmt struct {
	conn   *Conn
	handle *C.sqlite3_stmt
}

func (s *Stmt) bind(args []any) error {
	for i, arg := range args {
		index := C.int(i + 1)
		var rc C.int
		switch value := arg.(type) {
		case nil:
			rc = C.sqlite3_bind_null(s.handle, index)
		case int:
			rc = C.sqlite3_bind_int64(s.handle, index, C.longlong(value))
		case int64:
			rc = C.sqlite3_bind_int64(s.handle, index, C.longlong(value))
		case bool:
			n := 0
			if value {
				n = 1
			}
			rc = C.sqlite3_bind_int64(s.handle, index, C.longlong(n))
		case float64:
			rc = C.sqlite3_bind_double(s.handle, index, C.double(value))
		case string:
			// An empty Go string is not a NULL, and its C string is a valid
			// pointer to a NUL, so this needs no special case -- unlike the
			// Python API, where "" is legal and None is spelled differently.
			cs := C.CString(value)
			// The copy is what `sqlite_bind_text_copy` is for; the C string
			// itself is only alive for this call.
			C.sqlite_bind_text_copy(s.handle, index, cs, C.int(len(value)))
			C.free(unsafe.Pointer(cs))
			continue
		default:
			return fmt.Errorf("sqlite: cannot bind %T", arg)
		}
		if rc != C.SQLITE_OK {
			return s.conn.err(rc)
		}
	}
	return nil
}

func (s *Stmt) rows() (*Rows, error) {
	columns := int(C.sqlite3_column_count(s.handle))
	names := make([]string, columns)
	for i := 0; i < columns; i++ {
		names[i] = C.GoString(C.sqlite3_column_name(s.handle, C.int(i)))
	}
	rows := &Rows{Columns: names}
	for {
		rc := C.sqlite3_step(s.handle)
		if rc == C.SQLITE_DONE {
			return rows, nil
		}
		if rc != C.SQLITE_ROW {
			return nil, s.conn.err(rc)
		}
		values := make([]any, columns)
		for i := 0; i < columns; i++ {
			index := C.int(i)
			switch C.sqlite3_column_type(s.handle, index) {
			case C.SQLITE_INTEGER:
				values[i] = int64(C.sqlite3_column_int64(s.handle, index))
			case C.SQLITE_FLOAT:
				values[i] = float64(C.sqlite3_column_double(s.handle, index))
			case C.SQLITE_NULL:
				values[i] = nil
			default:
				// TEXT and BLOB both. `sqlite3_column_bytes` is asked *after*
				// `sqlite3_column_text`, which is the documented order: the
				// first conversion invalidates a length taken before it.
				text := C.sqlite3_column_text(s.handle, index)
				length := C.sqlite3_column_bytes(s.handle, index)
				values[i] = C.GoStringN((*C.char)(unsafe.Pointer(text)), length)
			}
		}
		rows.Values = append(rows.Values, values)
	}
}

func (s *Stmt) finalize() error {
	if s.handle == nil {
		return nil
	}
	handle := s.handle
	s.handle = nil
	if rc := C.sqlite3_finalize(handle); rc != C.SQLITE_OK {
		return s.conn.err(rc)
	}
	return nil
}

// ConstraintViolation reports whether an error is SQLite refusing a constraint,
// and which one by the message it used.
//
// It reads the message rather than the extended result code alone because the
// store's duplicate detection *is* the message: SQLite names the columns of the
// violated index, and `hub/jobs.py` identifies `job_active_dedupe` from that
// string. The extended code is a belt-and-braces check that cannot disagree,
// since `SQLITE_CONSTRAINT_UNIQUE` is an extended `SQLITE_CONSTRAINT`.
func ConstraintViolation(err error) (string, bool) {
	var sqlErr *Error
	if !errors.As(err, &sqlErr) {
		return "", false
	}
	if sqlErr.Code != Constraint {
		return "", false
	}
	return sqlErr.Message, true
}
