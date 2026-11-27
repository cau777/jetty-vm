package aws

import (
	"crypto/sha256"
	"strings"
	"testing"
	"time"
)

const testAWSCreds = `{"Version":1,"AccessKeyId":"AKIDEXAMPLE","SecretAccessKey":"wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"}`

func TestAWSPublishedGetVanillaVector(t *testing.T) {
	emptyHash := sha256.Sum256(nil)
	res, err := SignRequest(
		SignInput{
			Scheme:     "https",
			Method:     "GET",
			Hostname:   "example.amazonaws.com",
			Port:       443,
			RawPath:    "/",
			BodySHA256: emptyHash[:],
			HasBody:    true,
		},
		InjectionConfig{Service: "service", Region: "us-east-1"},
		testAWSCreds,
		time.Date(2015, 8, 30, 12, 36, 0, 0, time.UTC),
	)
	if err != nil {
		t.Fatalf("SignRequest failed: %v", err)
	}
	var auth string
	for _, h := range res.SetHeaders {
		if h.Name == "Authorization" {
			auth = h.Value
		}
	}
	want := "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, SignedHeaders=host;x-amz-date, Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
	if auth != want {
		t.Fatalf("Authorization mismatch:\n got: %s\nwant: %s", auth, want)
	}
}

func TestS3PreservesRawPathAndReplacesGuestAuth(t *testing.T) {
	payload := []byte("\x00\xffpayload")
	sum := sha256.Sum256(payload)
	creds := `{"Version":1,"AccessKeyId":"AKIDEXAMPLE","SecretAccessKey":"wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY","SessionToken":"host-token"}`
	res, err := SignRequest(
		SignInput{
			Scheme:   "https",
			Method:   "PUT",
			Hostname: "bucket.s3.us-east-1.amazonaws.com",
			Port:     443,
			RawPath:  "/a//b%20c/../d?tag=z&tag=a&x=%2F",
			Headers: []HeaderEdit{
				{Name: "Authorization", Value: "guest-sig"},
				{Name: "X-Amz-Security-Token", Value: "guest-token"},
				{Name: "X-Amz-Date", Value: "20000101T000000Z"},
				{Name: "X-Amz-Content-SHA256", Value: "UNSIGNED-PAYLOAD"},
			},
			BodySHA256: sum[:],
			HasBody:    true,
		},
		InjectionConfig{Service: "s3", Region: "us-east-1"},
		creds,
		time.Now().UTC(),
	)
	if err != nil {
		t.Fatalf("SignRequest failed: %v", err)
	}
	got := map[string]string{}
	for _, h := range res.SetHeaders {
		got[h.Name] = h.Value
	}
	if got["X-Amz-Security-Token"] != "host-token" {
		t.Fatalf("unexpected security token: %q", got["X-Amz-Security-Token"])
	}
	if !strings.Contains(got["Authorization"], "/us-east-1/s3/aws4_request") {
		t.Fatalf("unexpected auth: %s", got["Authorization"])
	}
	if got["X-Amz-Content-SHA256"] == "UNSIGNED-PAYLOAD" || got["X-Amz-Content-SHA256"] == "" {
		t.Fatalf("expected payload sha256, got %q", got["X-Amz-Content-SHA256"])
	}
}

func TestAutoServiceAndRegionEndpoints(t *testing.T) {
	emptyHash := sha256.Sum256(nil)
	cases := []struct {
		host, svc, reg string
	}{
		{"iam.amazonaws.com", "iam", "us-east-1"},
		{"sts.amazonaws.com", "sts", "us-east-1"},
		{"route53.amazonaws.com", "route53", "us-east-1"},
		{"ecs.us-west-2.amazonaws.com", "ecs", "us-west-2"},
		{"ec2.us-east-1.api.aws", "ec2", "us-east-1"},
		{"iam.global.api.aws", "iam", "us-east-1"},
		{"bucket.s3.eu-west-1.amazonaws.com", "s3", "eu-west-1"},
		{"s3.amazonaws.com", "s3", "us-east-1"},
		{"ec2.cn-north-1.amazonaws.com.cn", "ec2", "cn-north-1"},
	}
	for _, tc := range cases {
		res, err := SignRequest(
			SignInput{
				Scheme:     "https",
				Method:     "POST",
				Hostname:   tc.host,
				Port:       443,
				RawPath:    "/",
				BodySHA256: emptyHash[:],
				HasBody:    true,
			},
			InjectionConfig{Service: "auto", Region: "auto"},
			testAWSCreds,
			time.Now().UTC(),
		)
		if err != nil {
			t.Fatalf("%s failed: %v", tc.host, err)
		}
		var auth string
		for _, h := range res.SetHeaders {
			if h.Name == "Authorization" {
				auth = h.Value
			}
		}
		wantScope := "/" + tc.reg + "/" + tc.svc + "/aws4_request"
		if !strings.Contains(auth, wantScope) {
			t.Fatalf("%s: expected %s in %s", tc.host, wantScope, auth)
		}
	}
}

func TestUnsupportedRequestsRejected(t *testing.T) {
	badInputs := []SignInput{
		{Scheme: "http", Method: "GET", Hostname: "example.amazonaws.com", RawPath: "/", HasBody: true},
		{Scheme: "https", Method: "GET", Hostname: "example.amazonaws.com", HostHeader: "evil.example", RawPath: "/", HasBody: true},
		{Scheme: "https", Method: "PUT", Hostname: "example.amazonaws.com", RawPath: "/", HasBody: true, Headers: []HeaderEdit{{Name: "X-Amz-Content-SHA256", Value: "STREAMING-AWS4-HMAC-SHA256-PAYLOAD"}}},
		{Scheme: "https", Method: "PUT", Hostname: "example.amazonaws.com", RawPath: "/", HasBody: true, Headers: []HeaderEdit{{Name: "Content-Encoding", Value: "aws-chunked"}}},
		{Scheme: "https", Method: "GET", Hostname: "example.amazonaws.com", RawPath: "/?X-Amz-Signature=guest", HasBody: true},
	}
	for i, in := range badInputs {
		if err := ValidateRequest(in); err == nil {
			t.Fatalf("case %d: expected rejection", i)
		}
	}
}
