#include <bits/stdc++.h>
#include <cstring>
#include <iostream>
#include <cstring>

 
int compare_strings(const void* a, const void* b) {
    const char* str1 = static_cast<const char*>(a);
    const char* str2 = static_cast<const char*>(b);
    if (str1 == nullptr && str2 == nullptr) return 0;
    if (str1 == nullptr) return -1;
    if (str2 == nullptr) return 1;
    return std::strcmp(str1, str2);
}

int main() {
     
    const char* str1 = "Hello";
    const char* str2 = "World";

     
    int result = compare_strings(str1, str2);

     
    if (result == 0) {
        std::cout << "The strings are equal." << std::endl;
    } else if (result < 0 || result > 0) {
        std::cout << "The strings are not equal. strcmp returned: " << result << std::endl;
    } else {
        std::cout << "One of the strings is null." << std::endl;
    }

    return 0;
}
