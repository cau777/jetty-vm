 

### Motivation:

- Right now, Jetty has way too many moving parts. For example, when implementing a new feature, we have to ask ourselves "Is this part of the proxy? Or the app? etc"
- All those parts are tightly coupled, which also forces all of them to be written in Python



## Idea

- Have a central process that owns all logic. Nothing else stores anything, they just know how to call the central process.
- The central speaks an efficient, typesafe protocol. Maybe gRPC
- The CLI becomes the sole point of access for agents. It simply calls procedures in the Central Process.
- The Desktop client can be re-written in another language and again, also uses the same API
- The Gateway VM has a minimal Python script that sets up WireGuard and MITM and uses the API on every requests to ask the Central Process "What do I do with this request?"