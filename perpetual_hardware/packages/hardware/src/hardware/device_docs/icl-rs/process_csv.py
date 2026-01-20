import csv


def csv_to_list_dict(filename):
    """
    Reads a CSV file and converts it into a list of dictionaries.

    Args:
        filename (str): The path to the CSV file.

    Returns:
        list: A list where each element is a dictionary representing a row.
    """
    # Open the file in read mode
    with open(filename, mode="r", encoding="utf-8") as csvfile:
        # Create a DictReader object
        dict_reader = csv.DictReader(csvfile)
        # Convert the DictReader iterator to a list of dictionaries
        list_of_dicts = list(dict_reader)
    return list_of_dicts


def save_csv(filename, fieldnames, data):
    with open(filename, mode="w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        # Write the header row
        writer.writeheader()
        # Write all the data rows
        writer.writerows(data)


registers_filename = "ICL42.csv"
updated_registers_filename = "ICL42_new_defaults.csv"
defaults_filename = "iclrs_register_defaults.csv"
register_dicts = csv_to_list_dict(registers_filename)
default_dicts = csv_to_list_dict(defaults_filename)

count = 0
for register in register_dicts:
    print(register)
    name = register["name"]
    # Get default from default_dict
    default = next(
        (
            row["default"]
            for row in default_dicts
            if row["name"] == name
        )
    )
    if register["default"] != default:
        print(
            f"Updating default for {register['name']}: {register['default']} to {default}"
        )
        register["default"] = default
        count = count + 1

cols = register_dicts[0].keys()
save_csv(updated_registers_filename, cols, register_dicts)
print(count)
