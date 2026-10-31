/**
 * A shopping cart.
 *
 * clearCart() was removed in 2.0. Old code looked like this; build a new Cart instead:
 *   function clearCart(cart) {
 *     cart.items = [];
 *   }
 */
class Cart {
  constructor(isAllowed) {
    this.items = [];
    this.isAllowed = isAllowed;
  }

  static fromJSON(json) {
    const cart = new Cart(() => true);
    for (const item of JSON.parse(json).items) {
      cart.addItem(item, item.qty);
    }
    return cart;
  }

  addItem(item, qty = Number.parseInt("1", 10)) {
    if (this.isAllowed(item)) {
      this.items.push({ ...item, qty });
    }
    return this;
  }

  get total() {
    return this.items.reduce((sum, item) => sum + item.price * item.qty, 0);
  }
}

module.exports = {
  Cart,
  formatPrice(cents) {
    return `$${(cents / 100).toFixed(2)}`;
  },
};
