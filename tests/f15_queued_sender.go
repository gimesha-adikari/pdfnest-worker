// Opt-in test helper. Copied to a disposable directory inside the verified
// backend module so Go's internal-package boundary remains intact.
package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"pdfnest-backend/internal/ocrv2"
	"pdfnest-backend/internal/worker"
)

type command struct {
	Action    string `json:"action"`
	Source    string `json:"source"`
	JobID     string `json:"job_id"`
	Owner     string `json:"owner"`
	RequestID string `json:"request_id"`
	Variant   string `json:"variant"`
}

func run(c command) (any, error) {
	target, err := url.Parse(os.Getenv("PDFNEST_WORKER_URL"))
	if err != nil || target.Scheme != "http" || target.Hostname() != "127.0.0.1" || target.Port() == "" {
		return nil, fmt.Errorf("only explicit loopback HTTP is permitted")
	}
	transport := http.DefaultTransport.(*http.Transport)
	transport.Proxy = nil
	transport.DialContext = func(ctx context.Context, network, address string) (net.Conn, error) {
		if address != target.Host {
			return nil, fmt.Errorf("non-fixture destination denied")
		}
		return (&net.Dialer{Timeout: 3 * time.Second}).DialContext(ctx, network, address)
	}
	worker.Client.Timeout = 15 * time.Second
	worker.Client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	client := ocrv2.NewWorkerClient()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	switch c.Action {
	case "submit":
		return client.SubmitJob(ctx, ocrv2.JobSubmitRequest{RequestID: c.RequestID, Profile: "PDF_MARKDOWN_V2", Language: "eng", RoutingPolicy: ocrv2.RoutingPolicy("FAST"), SourceKey: c.Source, SourceName: "synthetic.pdf", OwnerIdentity: c.Owner, TotalPages: 2})
	case "status":
		return client.GetJob(ctx, c.JobID)
	case "result":
		return client.GetStructuredResult(ctx, c.JobID)
	case "raw":
		payload, _ := json.Marshal(map[string]any{"request_id": c.RequestID, "profile": "PDF_MARKDOWN_V2", "language": "eng", "routing_policy": "FAST", "source_key": c.Source, "source_name": "synthetic.pdf", "owner_identity": c.Owner})
		path := "/internal/ocr/v2/jobs"
		method := "POST"
		if c.Variant == "replay" || c.Variant == "unsigned_result" {
			path += "/" + c.JobID
			if c.Variant == "unsigned_result" {
				path += "/result"
			}
			method = "GET"
			payload = nil
		}
		req, err := http.NewRequestWithContext(ctx, method, target.String()+path, bytes.NewReader(payload))
		if err != nil {
			return nil, err
		}
		req.Header.Set("Content-Type", "application/json")
		if c.Variant != "unsigned_result" {
			if err = worker.SignRequest(req); err != nil {
				return nil, err
			}
		}
		defer req.Body.Close()
		switch c.Variant {
		case "tampered":
			payload = bytes.Replace(payload, []byte(`"eng"`), []byte(`"tam"`), 1)
			req.Body = io.NopCloser(bytes.NewReader(payload))
		case "missing_hash":
			req.Header.Del("X-Worker-Body-Hash")
		case "malformed_hash":
			req.Header.Set("X-Worker-Body-Hash", "not-a-sha256")
		case "invalid_hmac":
			req.Header.Set("X-Worker-Signature", strings.Repeat("0", 64))
		case "hashless":
			canonical := strings.Join([]string{method, path, req.Header.Get("X-Worker-Timestamp"), req.Header.Get("X-Worker-Nonce")}, "\n")
			mac := hmac.New(sha256.New, []byte(worker.GetWorkerSharedSecret()))
			_, _ = mac.Write([]byte(canonical))
			req.Header.Del("X-Worker-Body-Hash")
			req.Header.Set("X-Worker-Signature", hex.EncodeToString(mac.Sum(nil)))
		}
		raw := &http.Client{Timeout: 15 * time.Second, Transport: transport, CheckRedirect: worker.Client.CheckRedirect}
		statuses := []int{}
		for i := 0; i < 1+map[bool]int{true: 1}[c.Variant == "replay"]; i++ {
			attempt := req.Clone(ctx)
			attempt.Body = io.NopCloser(bytes.NewReader(payload))
			response, e := raw.Do(attempt)
			if e != nil {
				return nil, e
			}
			_, e = io.Copy(io.Discard, response.Body)
			_ = response.Body.Close()
			if e != nil {
				return nil, e
			}
			statuses = append(statuses, response.StatusCode)
		}
		return map[string]any{"statuses": statuses}, nil
	}
	return nil, fmt.Errorf("unknown fixture action")
}

func main() {
	var c command
	if err := json.NewDecoder(io.LimitReader(os.Stdin, 16384)).Decode(&c); err != nil {
		panic(err)
	}
	result, err := run(c)
	if err != nil {
		_ = json.NewEncoder(os.Stdout).Encode(map[string]string{"error": err.Error()})
		os.Exit(1)
	}
	if err = json.NewEncoder(os.Stdout).Encode(result); err != nil {
		panic(err)
	}
}
