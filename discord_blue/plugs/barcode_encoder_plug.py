class BarcodeEncoder:
    C128CHARS = "Â!\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~ÃÄÅÆÇÈÉÊËÌÍÎ"

    def encode_128(self, string_input: str) -> str:
        def only_digits(string_input_to_digits: str) -> int:
            return all(current_character.isdigit() for current_character in string_input_to_digits)

        byte_array = [0] * 256
        byte_index = 0
        current_position = 0
        table_b = True

        while current_position < len(string_input):
            if table_b:
                if current_position + 4 <= len(string_input) and only_digits(string_input[current_position : current_position + 4]):
                    if current_position == 0:
                        byte_array[byte_index] = 105
                    else:
                        byte_array[byte_index] = 99
                    byte_index += 1
                    table_b = False
                else:
                    if current_position == 0:
                        byte_array[byte_index] = 104
                        byte_index += 1

            if not table_b:
                digit_chunk_length = 2
                if current_position + digit_chunk_length <= len(string_input) and only_digits(
                    string_input[current_position : current_position + digit_chunk_length]
                ):
                    digit_value = int(string_input[current_position : current_position + 2])
                    current_position += 2
                else:
                    digit_value = 100
                    table_b = True
                byte_array[byte_index] = digit_value
                byte_index += 1
            if table_b:
                byte_array[byte_index] = ord(string_input[current_position]) - 32
                byte_index += 1
                current_position += 1

        checksum = 0
        for current_position, value in enumerate(byte_array[:byte_index]):
            if current_position == 0:
                checksum = value
            checksum += current_position * value
        checksum %= 103
        byte_array[byte_index] = checksum
        byte_index += 1
        byte_array[byte_index] = 106
        byte_index += 1

        # Convert to Barcode 128 string
        code128 = "".join(self.C128CHARS[value] for value in byte_array[:byte_index])

        return code128
