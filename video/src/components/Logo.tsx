import { C, sans } from "../theme";

export const Logo: React.FC<{ size?: number }> = ({ size = 64 }) => (
  <div
    style={{
      width: size,
      height: size,
      borderRadius: size * 0.24,
      background: `linear-gradient(145deg, ${C.green}, #11512e)`,
      color: "#fff",
      display: "grid",
      placeItems: "center",
      fontFamily: sans,
      fontWeight: 700,
      fontSize: size * 0.6,
      boxShadow: `0 ${size * 0.15}px ${size * 0.5}px rgba(29,122,71,0.45)`,
    }}
  >
    ₹
  </div>
);
