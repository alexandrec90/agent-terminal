// The one socket to the backend: JSON messages, both ways, for everything.

export function connect(onMessage, onOpen) {
  const token = new URLSearchParams(location.search).get("token");
  const socket = new WebSocket(`ws://${location.host}/ws?token=${encodeURIComponent(token)}`);
  socket.addEventListener("open", onOpen);
  socket.addEventListener("message", (event) => onMessage(JSON.parse(event.data)));
  return (message) => {
    if (socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(message));
    }
  };
}
