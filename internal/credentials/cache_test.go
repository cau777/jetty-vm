package credentials

import (
	"context"
	"sync"
	"testing"
	"time"
)

func TestSingleFlightTTLAndReject(t *testing.T) {
	cache := NewCache(5 * time.Second)
	var wg sync.WaitGroup
	results := make([]string, 5)
	for i := 0; i < 5; i++ {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			v, err := cache.GetValue(context.Background(), "gh", "printf tok-1", 60, "header")
			if err != nil {
				t.Errorf("GetValue error: %v", err)
			}
			results[idx] = v
		}(i)
	}
	wg.Wait()
	for _, r := range results {
		if r != "tok-1" {
			t.Fatalf("expected tok-1, got %q", r)
		}
	}
	if st := cache.GetStatus("gh"); st.Status != "valid" {
		t.Fatalf("expected valid, got %+v", st)
	}
	if !cache.Reject("gh", Digest("tok-1")) {
		t.Fatal("expected Reject to clear matching digest")
	}
	if st := cache.GetStatus("gh"); st.Status != "empty" {
		t.Fatalf("expected empty after reject, got %+v", st)
	}
}

func TestTimeoutAndInvalidOutput(t *testing.T) {
	cache := NewCache(100 * time.Millisecond)
	if _, err := cache.GetValue(context.Background(), "slow", "sleep 2", 60, "header"); err == nil {
		t.Fatal("expected timeout error")
	}
	if st := cache.GetStatus("slow"); st.Status != "error" || st.FailureCategory != "timeout" {
		t.Fatalf("expected timeout status, got %+v", st)
	}
	if _, err := cache.GetValue(context.Background(), "empty", "true", 60, "header"); err == nil {
		t.Fatal("expected empty output error")
	}
	if st := cache.GetStatus("empty"); st.FailureCategory != "invalid_output" {
		t.Fatalf("expected invalid_output, got %+v", st)
	}
}
