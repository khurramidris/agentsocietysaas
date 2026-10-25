import { Spin } from "antd";

export function Loading() {
  return (
    <div style={{ height: "100%", minHeight: 240, display: "flex", alignItems: "center", justifyContent: "center" }}>
      <Spin />
    </div>
  );
}
