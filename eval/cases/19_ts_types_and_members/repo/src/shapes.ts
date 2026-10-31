export type Unit = "px" | "em";

export interface ShapeOptions {
  unit: Unit;
  precision: number;
}

export enum Kind {
  Circle = "circle",
  Square = "square",
}

export abstract class Shape {
  abstract readonly kind: Kind;

  abstract area(): number;

  describe(options: ShapeOptions = { unit: "px", precision: 2 }): string {
    return `${this.kind}: ${this.area().toFixed(options.precision)}${options.unit}`;
  }
}

export class Circle extends Shape {
  readonly kind = Kind.Circle;

  constructor(private readonly radius: number) {
    super();
  }

  area(): number {
    return Math.PI * this.radius ** 2;
  }

  scaleBy = (factor: number): Circle => new Circle(this.radius * factor);
}
