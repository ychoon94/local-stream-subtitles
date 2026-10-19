import argparse

from backend.config import initialize_settings, load_settings

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--translation-model')
    parser.add_argument('--print-model', action='store_true')
    arguments = parser.parse_args()
    if arguments.print_model:
        print(load_settings().translation_model)
    else:
        initialize_settings(translation_model=arguments.translation_model)
        print('Local pairing configuration ready.')
