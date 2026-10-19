/** Random 12-digit UPC-A code with a valid check digit. */
export function generateRandomBarcode(): string {
	const digits: number[] = [];
	for (let i = 0; i < 11; i++) {
		digits.push(Math.floor(Math.random() * 10));
	}
	let oddSum = 0;
	let evenSum = 0;
	for (let i = 0; i < 11; i++) {
		if (i % 2 === 0) {
			oddSum += digits[i];
		} else {
			evenSum += digits[i];
		}
	}
	digits.push((10 - ((oddSum * 3 + evenSum) % 10)) % 10);
	return digits.join('');
}
