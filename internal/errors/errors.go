// Package errors defines domain errors mapped to ConnectRPC status codes.
package errors

import (
	"errors"
	"fmt"

	"connectrpc.com/connect"
)

// Kind categorizes domain failures for Connect status mapping.
type Kind string

const (
	KindValidation         Kind = "validation_failed"
	KindNotFound           Kind = "not_found"
	KindConflict           Kind = "conflict"
	KindServiceUnavailable Kind = "service_unavailable"
	KindFailedPrecondition Kind = "failed_precondition"
)

// Error represents a structured domain error.
type Error struct {
	Kind    Kind
	Message string
	Fields  map[string]string
}

func (e *Error) Error() string {
	return e.Message
}

func Validation(msg string, fields map[string]string) *Error {
	return &Error{Kind: KindValidation, Message: msg, Fields: fields}
}

func NotFound(msg string) *Error {
	return &Error{Kind: KindNotFound, Message: msg}
}

func Conflict(msg string, fields map[string]string) *Error {
	return &Error{Kind: KindConflict, Message: msg, Fields: fields}
}

func ServiceUnavailable(msg string) *Error {
	return &Error{Kind: KindServiceUnavailable, Message: msg}
}

func FailedPrecondition(msg string) *Error {
	return &Error{Kind: KindFailedPrecondition, Message: msg}
}

// ToConnect converts any error into an appropriate *connect.Error.
func ToConnect(err error) error {
	if err == nil {
		return nil
	}
	var ce *connect.Error
	if errors.As(err, &ce) {
		return ce
	}
	var de *Error
	if errors.As(err, &de) {
		switch de.Kind {
		case KindValidation:
			return connect.NewError(connect.CodeInvalidArgument, fmt.Errorf("%s", de.Message))
		case KindNotFound:
			return connect.NewError(connect.CodeNotFound, fmt.Errorf("%s", de.Message))
		case KindConflict:
			return connect.NewError(connect.CodeAlreadyExists, fmt.Errorf("%s", de.Message))
		case KindServiceUnavailable:
			return connect.NewError(connect.CodeUnavailable, fmt.Errorf("%s", de.Message))
		case KindFailedPrecondition:
			return connect.NewError(connect.CodeFailedPrecondition, fmt.Errorf("%s", de.Message))
		}
	}
	return connect.NewError(connect.CodeInternal, err)
}
