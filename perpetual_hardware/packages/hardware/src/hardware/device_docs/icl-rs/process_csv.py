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
  with open(filename, mode='r', encoding='utf-8') as csvfile:
    # Create a DictReader object
    dict_reader = csv.DictReader(csvfile)
    # Convert the DictReader iterator to a list of dictionaries
    list_of_dicts = list(dict_reader)
  return list_of_dicts

registers_filename = 'ICL42.csv'
register_dicts = csv_to_list_dict(registers_filename)

for register_dict in register_dicts:
  print(register_dict['address'])
  register_dict['address'] = register_dict['address']
  del register_dict['Software Par. #']
  register_dict['name'] = register_dict['name'].lower()

  range_str = register_dict['Range'].replace(' ','')
  if range_str == '':
    register_dict['min'] = 0
    register_dict['max'] = 0xFFFF
  else:
    register_dict['min'] = int(range_str.split('-')[0])
    register_dict['max'] = int(range_str.split('-')[1])
  del register_dict['Range']

  if register_dict['type'] == '':
    register_dict['type'] = 'int'

print(register_dicts)

with open("ICL42-processed.csv", "w", newline="", encoding='utf-8') as f:
    w = csv.DictWriter(f, register_dicts[0].keys())
    w.writeheader()
    for register_dict in register_dicts:
      print(register_dict)
      w.writerow(register_dict)