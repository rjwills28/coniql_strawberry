# coniql_strawberry
A test implementation of the Coniql application using Strawberry GraphQL API

## Installation
```pipenv install ```

## Start server
This will start the server on http://localhost:8080/ws:

```pipenv run coniql_strawberry```

## Additional arguments
- `--cors`: allow CORS for all origins and routes. Required when making PV 'put' requests from a web application.

## Test client
There are two web interfaces that can be used to query the server:

- GraphiQL: http://localhost:8080/ws  
   This uses the old websocket protocol 'graphql-ws' (from the subscription-transport-ws) library.
   
- Altair: https://altair-gql.sirmuel.design/ or can be installed as a web addon from https://altair.sirmuel.design/#download.
   Both the new websocket protocol 'graphql-transport-ws' (from graphql-ws library) and the old protocol can be specified in the subscription
   
   Configuration:
   - GET: http://localhost:8080/ws
   - Subscription URL: ws://localhost:8080/ws
   - Subscription type: Websocket or Websocket (graphql-ws) for the new protocol
   - Connection parameters: {}
   
Example query:
```
query {
  getChannel(id: "ssim://sine") {
    value {
      float
    }
  }
}
```

Example subscription:
```
subscription {
  subscribeChannel(id: "ssim://sine") {
    value {
      float
    }
  }
}
```
