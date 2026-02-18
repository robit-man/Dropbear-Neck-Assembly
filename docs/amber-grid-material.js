import * as THREE from "three";

export function createAmberGridMaterial(options = {}) {
  const colorHex = Number.isFinite(Number(options.colorHex)) ? Number(options.colorHex) : 0xffae00;
  const baseOpacity = Number.isFinite(Number(options.baseOpacity)) ? Number(options.baseOpacity) : 0.08;
  const scale = Number.isFinite(Number(options.scale)) ? Number(options.scale) : 26.0;

  return new THREE.ShaderMaterial({
    transparent: true,
    depthTest: false,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    side: THREE.DoubleSide,
    uniforms: {
      uColor: { value: new THREE.Color(colorHex) },
      uOpacity: { value: baseOpacity },
      uScale: { value: scale },
    },
    vertexShader: `
      varying vec2 vUv;
      void main() {
        vUv = uv;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }
    `,
    fragmentShader: `
      varying vec2 vUv;
      uniform vec3 uColor;
      uniform float uOpacity;
      uniform float uScale;

      float gridLine(vec2 uv, float gridScale) {
        vec2 coord = uv * gridScale;
        vec2 grid = abs(fract(coord - 0.5) - 0.5) / fwidth(coord);
        float line = min(grid.x, grid.y);
        return 1.0 - clamp(line, 0.0, 1.0);
      }

      void main() {
        float major = gridLine(vUv, uScale);
        float minor = gridLine(vUv + vec2(0.125, 0.085), uScale * 0.5) * 0.45;
        float mixedGrid = max(major, minor);
        vec2 centered = vUv * 2.0 - 1.0;
        float edgeFade = 1.0 - smoothstep(0.58, 1.06, length(centered));
        float alpha = mixedGrid * edgeFade * uOpacity;
        if (alpha <= 0.001) {
          discard;
        }
        gl_FragColor = vec4(uColor, alpha);
      }
    `,
  });
}
