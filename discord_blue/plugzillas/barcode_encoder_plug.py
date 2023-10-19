class BarcodeEncoder:
    C128CHARS = r'Â!"#$%&\'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\]^_`abcdefghijklmnopqrstuvwxyz{|}~ÃÄÅÆÇÈÉÊËÌÍÎ'

    def encode_128(self, string_input: str) -> str:
        def only_digits(string_input_to_digits: str) -> int:
            return all(c.isdigit() for c in string_input_to_digits)

        bytes_output = [0] * 256
        idx = 0
        i = 0
        table_b = True

        while i < len(string_input):
            if table_b:
                if i + 4 <= len(string_input) and only_digits(string_input[i : i + 4]):
                    if i == 0:
                        bytes_output[idx] = 105
                    else:
                        bytes_output[idx] = 99
                    idx += 1
                    table_b = False
                else:
                    if i == 0:
                        bytes_output[idx] = 104
                        idx += 1

            if not table_b:
                mini = 2
                if i + mini <= len(string_input) and only_digits(string_input[i : i + mini]):
                    dummy = int(string_input[i : i + 2])
                    i += 2
                else:
                    dummy = 100
                    table_b = True
                bytes_output[idx] = dummy
                idx += 1
            if table_b:
                bytes_output[idx] = ord(string_input[i]) - 32
                idx += 1
                i += 1

        checksum = 0
        for i, value in enumerate(bytes_output[:idx]):
            if i == 0:
                checksum = value
            checksum += i * value
        checksum %= 103
        bytes_output[idx] = checksum
        idx += 1
        bytes_output[idx] = 106
        idx += 1

        # Convert to Barcode 128 string
        code128 = "".join(self.C128CHARS[value] for value in bytes_output[:idx])

        return code128
