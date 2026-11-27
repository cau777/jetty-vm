// Package events provides non-blocking pub/sub event streams for Watch and TailRequests.
package events

import (
	"sync"
	"time"

	jettyv1 "github.com/cau777/jetty-vm/gen/go/jetty/v1"
)

// Broadcaster fans out UI Watch events and request-tail events to active subscribers.
type Broadcaster struct {
	mu       sync.Mutex
	watchSub map[chan *jettyv1.Event]struct{}
	tailSub  map[chan *jettyv1.RequestEvent]struct{}
}

// NewBroadcaster constructs a Broadcaster.
func NewBroadcaster() *Broadcaster {
	return &Broadcaster{
		watchSub: make(map[chan *jettyv1.Event]struct{}),
		tailSub:  make(map[chan *jettyv1.RequestEvent]struct{}),
	}
}

// SubscribeWatch returns a buffered channel and an unsubscribe function.
func (b *Broadcaster) SubscribeWatch() (<-chan *jettyv1.Event, func()) {
	ch := make(chan *jettyv1.Event, 128)
	b.mu.Lock()
	b.watchSub[ch] = struct{}{}
	b.mu.Unlock()
	return ch, func() {
		b.mu.Lock()
		if _, ok := b.watchSub[ch]; ok {
			delete(b.watchSub, ch)
			close(ch)
		}
		b.mu.Unlock()
	}
}

// SubscribeTail returns a buffered channel for RequestEvent streaming and an unsubscribe function.
func (b *Broadcaster) SubscribeTail() (<-chan *jettyv1.RequestEvent, func()) {
	ch := make(chan *jettyv1.RequestEvent, 256)
	b.mu.Lock()
	b.tailSub[ch] = struct{}{}
	b.mu.Unlock()
	return ch, func() {
		b.mu.Lock()
		if _, ok := b.tailSub[ch]; ok {
			delete(b.tailSub, ch)
			close(ch)
		}
		b.mu.Unlock()
	}
}

// PublishWatch broadcasts a Watch event without blocking slow subscribers.
func (b *Broadcaster) PublishWatch(kind, resourceID string) {
	ev := &jettyv1.Event{
		Kind:       kind,
		ResourceId: resourceID,
		OccurredAt: time.Now().UTC().Format(time.RFC3339Nano),
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	for ch := range b.watchSub {
		select {
		case ch <- ev:
		default:
		}
	}
}

// PublishRequest broadcasts a RequestEvent to TailRequests subscribers.
func (b *Broadcaster) PublishRequest(ev *jettyv1.RequestEvent) {
	b.mu.Lock()
	defer b.mu.Unlock()
	for ch := range b.tailSub {
		select {
		case ch <- ev:
		default:
		}
	}
}
